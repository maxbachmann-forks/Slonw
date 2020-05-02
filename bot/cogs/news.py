import logging
import typing as t
from datetime import date, datetime

import discord
import feedparser
from bs4 import BeautifulSoup
from discord.ext.commands import Cog
from discord.ext.tasks import loop

from bot import constants
from bot.bot import Bot

PEPS_RSS_URL = "https://www.python.org/dev/peps/peps.rss/"

RECENT_THREADS_TEMPLATE = "https://mail.python.org/archives/list/{name}@python.org/recent-threads"
THREAD_TEMPLATE_URL = "https://mail.python.org/archives/api/list/{name}@python.org/thread/{id}/"
MAILMAN_PROFILE_URL = "https://mail.python.org/archives/users/{id}/"
THREAD_URL = "https://mail.python.org/archives/list/{list}@python.org/thread/{id}/"

AVATAR_URL = "https://www.python.org/static/opengraph-icon-200x200.png"

log = logging.getLogger(__name__)


class News(Cog):
    """Post new PEPs and Python News to `#python-news`."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self.webhook_names = {}
        self.webhook: t.Optional[discord.Webhook] = None
        self.channel: t.Optional[discord.TextChannel] = None

        self.bot.loop.create_task(self.get_webhook_names())
        self.bot.loop.create_task(self.get_webhook_and_channel())

    async def start_tasks(self) -> None:
        """Start the tasks for fetching new PEPs and mailing list messages."""
        self.fetch_new_media.start()

    @loop(minutes=20)
    async def fetch_new_media(self) -> None:
        """Fetch new mailing list messages and then new PEPs."""
        await self.post_maillist_news()
        await self.post_pep_news()

    async def sync_maillists(self) -> None:
        """Sync currently in-use maillists with API."""
        # Wait until guild is available to avoid running before everything is ready
        await self.bot.wait_until_guild_available()

        response = await self.bot.api_client.get("bot/bot-settings/news")
        for mail in constants.PythonNews.mail_lists:
            if mail not in response["data"]:
                response["data"][mail] = []

        # Because we are handling PEPs differently, we don't include it to mail lists
        if "pep" not in response["data"]:
            response["data"]["pep"] = []

        await self.bot.api_client.put("bot/bot-settings/news", json=response)

    async def get_webhook_names(self) -> None:
        """Get webhook author names from maillist API."""
        await self.bot.wait_until_guild_available()

        async with self.bot.http_session.get("https://mail.python.org/archives/api/lists") as resp:
            lists = await resp.json()

        for mail in lists:
            if mail["name"].split("@")[0] in constants.PythonNews.mail_lists:
                self.webhook_names[mail["name"].split("@")[0]] = mail["display_name"]

    async def post_pep_news(self) -> None:
        """Fetch new PEPs and when they don't have announcement in #python-news, create it."""
        # Wait until everything is ready and http_session available
        await self.bot.wait_until_guild_available()
        await self.sync_maillists()

        async with self.bot.http_session.get(PEPS_RSS_URL) as resp:
            data = feedparser.parse(await resp.text("utf-8"))

        news_listing = await self.bot.api_client.get("bot/bot-settings/news")
        payload = news_listing.copy()
        pep_numbers = news_listing["data"]["pep"]

        # Reverse entries to send oldest first
        data["entries"].reverse()
        for new in data["entries"]:
            try:
                new_datetime = datetime.strptime(new["published"], "%a, %d %b %Y %X %Z")
            except ValueError:
                log.warning(f"Wrong datetime format passed in PEP new: {new['published']}")
                continue
            pep_nr = new["title"].split(":")[0].split()[1]
            if (
                    pep_nr in pep_numbers
                    or new_datetime.date() < date.today()
            ):
                continue

            msg = await self.send_webhook(
                title=new["title"],
                description=new["summary"],
                timestamp=new_datetime,
                url=new["link"],
                webhook_profile_name=data["feed"]["title"],
                footer=data["feed"]["title"]
            )
            payload["data"]["pep"].append(pep_nr)

            if msg.channel.is_news():
                log.trace("Publishing PEP annnouncement because it was in a news channel")
                await msg.publish()

        # Apply new sent news to DB to avoid duplicate sending
        await self.bot.api_client.put("bot/bot-settings/news", json=payload)

    async def post_maillist_news(self) -> None:
        """Send new maillist threads to #python-news that is listed in configuration."""
        await self.bot.wait_until_guild_available()
        await self.sync_maillists()
        existing_news = await self.bot.api_client.get("bot/bot-settings/news")
        payload = existing_news.copy()

        for maillist in constants.PythonNews.mail_lists:
            async with self.bot.http_session.get(RECENT_THREADS_TEMPLATE.format(name=maillist)) as resp:
                recents = BeautifulSoup(await resp.text(), features="lxml")

            # When response have <p>, this mean that no threads available
            if recents.p:
                continue

            for thread in recents.html.body.div.find_all("a", href=True):
                # We want only these threads that have identifiers
                if "latest" in thread["href"]:
                    continue

                thread_information, email_information = await self.get_thread_and_first_mail(
                    maillist, thread["href"].split("/")[-2]
                )

                try:
                    new_date = datetime.strptime(email_information["date"], "%Y-%m-%dT%X%z")
                except ValueError:
                    log.warning(f"Invalid datetime from Thread email: {email_information['date']}")
                    continue

                if (
                        thread_information["thread_id"] in existing_news["data"][maillist]
                        or new_date.date() < date.today()
                ):
                    continue

                content = email_information["content"]
                link = THREAD_URL.format(id=thread["href"].split("/")[-2], list=maillist)
                msg = await self.send_webhook(
                    title=thread_information["subject"],
                    description=content[:500] + f"... [continue reading]({link})" if len(content) > 500 else content,
                    timestamp=new_date,
                    url=link,
                    author=f"{email_information['sender_name']} ({email_information['sender']['address']})",
                    author_url=MAILMAN_PROFILE_URL.format(id=email_information["sender"]["mailman_id"]),
                    webhook_profile_name=self.webhook_names[maillist],
                    footer=f"Posted to {self.webhook_names[maillist]}"
                )
                payload["data"][maillist].append(thread_information["thread_id"])

                if msg.channel.is_news():
                    log.trace("Publishing mailing list message because it was in a news channel")
                    await msg.publish()

        await self.bot.api_client.put("bot/bot-settings/news", json=payload)

    async def send_webhook(self,
                           title: str,
                           description: str,
                           timestamp: datetime,
                           url: str,
                           webhook_profile_name: str,
                           footer: str,
                           author: t.Optional[str] = None,
                           author_url: t.Optional[str] = None,
                           ) -> discord.Message:
        """Send webhook entry and return sent message."""
        embed = discord.Embed(
            title=title,
            description=description,
            timestamp=timestamp,
            url=url,
            colour=constants.Colours.soft_green
        )
        if author and author_url:
            embed.set_author(
                name=author,
                url=author_url
            )
        embed.set_footer(text=footer, icon_url=AVATAR_URL)

        return await self.webhook.send(
            embed=embed,
            username=webhook_profile_name,
            avatar_url=AVATAR_URL,
            wait=True
        )

    async def get_thread_and_first_mail(self, maillist: str, thread_identifier: str) -> t.Tuple[t.Any, t.Any]:
        """Get mail thread and first mail from mail.python.org based on `maillist` and `thread_identifier`."""
        async with self.bot.http_session.get(
                THREAD_TEMPLATE_URL.format(name=maillist, id=thread_identifier)
        ) as resp:
            thread_information = await resp.json()

        async with self.bot.http_session.get(thread_information["starting_email"]) as resp:
            email_information = await resp.json()
        return thread_information, email_information

    async def get_webhook_and_channel(self) -> None:
        """Storage #python-news channel Webhook and `TextChannel` to `News.webhook` and `channel`."""
        await self.bot.wait_until_guild_available()
        self.webhook = await self.bot.fetch_webhook(constants.PythonNews.webhook)
        self.channel = await self.bot.fetch_channel(constants.PythonNews.channel)

        await self.start_tasks()

    def cog_unload(self) -> None:
        """Stop news posting tasks on cog unload."""
        self.fetch_new_media.cancel()


def setup(bot: Bot) -> None:
    """Add `News` cog."""
    bot.add_cog(News(bot))
