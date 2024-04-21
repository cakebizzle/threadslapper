"""
RssWatcher,

Watches RSS feeds for new episodes then posts a discord message/thread/post.
"""

import json
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import feedparser
from discord import Bot, ChannelType, Color, Embed, ForumChannel, TextChannel, Thread
from discord.ext import commands, tasks
from markdownify import markdownify as md
from pydantic import BaseModel

from threadslapper.settings import RssFeedToChannel, Settings

settings = Settings()
log = settings.create_logger('RssWatcher')


class MalformedFeedDecodeError(Exception):
    pass


class ChannelData(BaseModel):
    channel_title: str
    channel_url: str
    channel_image_url: str
    channel_last_published: str


class EpisodeData(ChannelData):
    number: int
    title: str
    description: str
    image_url: str
    episode_url: str
    tags: list[str]  # this doesn't work, ignore it for now

    def get_description(self, truncate: bool = False):
        """
        Converts an HTML formatted document to markdown, limits text to 2000 in length.

        If truncate=true, it will cut the description at the first double line-return.
        """
        if not self.description:
            return "No description provided"
        desc = md(self.description)

        if truncate:
            if "\n\n" in desc:
                first_line_break = desc.index("\n\n")
                return desc[:first_line_break]
        if len(desc) > 2000:
            desc = f"{desc[:1997]}..."

        return desc

    def get_title(self, prefix: str = "", override_ep_number: bool = False) -> str:
        """
        Appends a prefix to a title to make it clear what feed it comes from,
        if the title of the episode does not contain an episode number, prepend
        the episode number in the RSS feed to the episode title.
        """
        if override_ep_number:
            return self.title.strip()
        if self.title.startswith(f"{self.number}"):
            return f"{prefix} {self.title}".strip()
        return f"{prefix} {self.number}: {self.title}".strip()

    def get_timestamp(self) -> datetime:
        """
        Attempt to convert a time format to a datetime object.
        """
        observed_datetime_formats = ["%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"]
        for dt_fmt in observed_datetime_formats:
            try:
                dt = datetime.strptime(self.channel_last_published, dt_fmt)
                return dt
            except:
                pass
        return datetime.now()


class RssWatcher(commands.Cog):
    def __init__(self, bot: Bot):
        self.bot = bot
        self.feeds = settings.get_channels_list()

        log.info(f'Beginning first time check of RSS feed... {", ".join([feed.title for feed in self.feeds])}')
        if settings.startup_latest_episode_check:
            for feed in self.feeds:
                if (latest_episode := self.check_rss(rss=feed)) is not None:
                    log.info(f'{feed.title}: Latest episode checked on bot power on: {latest_episode.number}.')
                else:
                    raise RuntimeError(f"{feed.title}: No episode data found! Please check RSS Feed URL")

    def startup_validate(self):
        """Check that configured channels are available"""
        log.info("STARTUP Checking that configured channels are available.")
        for feed in self.feeds:
            # log.info(feed.channel_list)
            if feed.enabled is False:
                log.info(f'{feed.title}: Is disabled, skipping.')
                continue
            for index, (_announce_channel, _channel) in enumerate(
                feed.get_channels(
                    settings.override_announce_channel_id,
                    settings.override_channel_id,
                )
            ):
                channel = self.bot.get_channel(_channel)
                if not channel:
                    log.warning(f"{feed.title}-{index}: Channel (id={_channel}) not found!")
                    continue
                log.info(f"{feed.title}-{index}: Found channel (id={_channel}): {channel.guild.name}/{channel.name}")

                announce_channel = self.bot.get_channel(_announce_channel)
                if announce_channel:
                    log.info(
                        f"{feed.title}-{index}: Found announcement channel (id={_announce_channel}): {channel.guild.name}/{announce_channel.name}"
                    )

    def cog_unload(self):
        self.check_rss_feed.cancel()

    def get_embed(self, feed: RssFeedToChannel, latest_episode: EpisodeData, truncate: bool = False) -> Embed:
        """
        Construct a cool Embed object. Much better looking than just putting text in.
        """
        embed = Embed(
            title=latest_episode.get_title(prefix=feed.title_prefix, override_ep_number=feed.override_episode_numbers),
            description=latest_episode.get_description(truncate=truncate),
            color=Color.from_rgb(*feed.get_color_theme()),
            url=latest_episode.episode_url,
            timestamp=latest_episode.get_timestamp(),
        )
        embed.set_footer(text=f"Tags: {', '.join(latest_episode.tags)}")
        # embed.set_thumbnail(url=latest_episode.channel_image_url)
        embed.set_author(name=latest_episode.channel_title, icon_url=latest_episode.channel_image_url)
        embed.set_image(url=latest_episode.image_url)

        return embed

    def _get_channel_info(self, rss: RssFeedToChannel, channel_info: dict[str, Any]) -> ChannelData:
        return ChannelData(
            channel_url=channel_info.get(rss.rss_channel_url_key, ''),
            channel_image_url=channel_info.get(rss.rss_channel_image_key, {}).get('href', ''),
            channel_last_published=channel_info.get(rss.rss_channel_last_published_key, ''),
            channel_title=channel_info.get(rss.rss_channel_title_key, ''),
        )

    def _get_rss_feedparser(self, rss: RssFeedToChannel) -> dict | None:
        data = dict(feedparser.parse(rss.rss_feed))
        with rss.get_tmp_feed_path(settings.log_path).open(mode="w") as f:
            if isinstance(data, dict):
                json.dump(data, f, indent=2)
            else:
                f.write(str(data))
        return data

    def _get_latest_episode_data(self, rss: RssFeedToChannel) -> EpisodeData:
        """Gets the data for the latest episode"""
        data = self._get_rss_feedparser(rss)

        channel_info = self._get_channel_info(rss, data.get('feed', {}))
        episode_list = data.get('entries', [])
        if len(episode_list) == 0:
            raise MalformedFeedDecodeError()
        latest_episode = episode_list[rss.get_latest_episode_index_position()]

        latest_ep_id = latest_episode.get(rss.rss_episode_key, 0)
        if rss.override_episode_numbers:
            latest_ep_id = len(data.get('entries', []))

        return_data = EpisodeData(
            number=latest_ep_id,
            title=latest_episode.get(rss.rss_title_key, "None"),
            episode_url=latest_episode.get(rss.rss_episode_url_key, "None"),
            description=latest_episode.get(rss.rss_description_key, "None"),
            image_url=latest_episode.get(rss.rss_image_key, {}).get('href', ''),
            tags=[tag.term for tag in latest_episode.get(rss.rss_tag_key, [])],
            channel_url=channel_info.channel_url,
            channel_image_url=channel_info.channel_image_url,
            channel_last_published=channel_info.channel_last_published,
            channel_title=channel_info.channel_title,
        )

        return return_data

    async def add_text_thread(
        self,
        channel: TextChannel,
        title: str,
        embed: Embed,
        latest_episode_number: int,
        feed_title: str,
        override_episode_check: bool = False,
    ) -> Thread | None:
        """If the channel is a regular text channel, spawn a thread"""
        if override_episode_check or not title in [thread.name for thread in channel.threads]:
            message = await channel.send(content=title, embed=embed, suppress=False)

            new_thread = await channel.create_thread(
                message=message,
                name=title,
                type=ChannelType.public_thread,
                reason=f"{feed_title}: New Episode ({latest_episode_number}) detected, creating thread: {title}",
            )
            await new_thread.join()
            if (first_msg_in_thread := new_thread.starting_message) is not None:
                await first_msg_in_thread.pin()

            log.info(f"{feed_title}: Thread '{channel.guild.name}/{title}' created!")
            return new_thread
        else:
            log.info(f"{feed_title}: Thread '{channel.guild.name}/{title}' already exists, returning thread object.")
            threads = [thread for thread in channel.threads if thread.name == title]
            if len(threads) == 1:
                return threads[0]

        return None

    async def add_forum_thread(
        self,
        channel: ForumChannel,
        title: str,
        embed: Embed,
        latest_episode_number: int,
        feed_title: str,
        override_episode_check: bool = False,
    ) -> Thread | None:
        """If the channel is a Forum, spawn a post (that is actually a thread)"""
        if override_episode_check or not title in [thread.name for thread in channel.threads]:
            new_thread = await channel.create_thread(
                name=title,
                embed=embed,
                reason=f"{feed_title}: New Episode ({latest_episode_number}) detected, creating thread: {title}",
            )
            await new_thread.join()
            # if (message := new_thread.starting_message) is not None:
            #     await message.publish()
            # if (first_msg_in_thread := new_thread.starting_message) is not None:
            #     await first_msg_in_thread.pin()

            log.info(f"{feed_title}: Channel '{channel.guild.name}/{title}' created!")
            return new_thread
        else:
            log.info(f"{feed_title}: Thread '{channel.guild.name}/{title}' already exists, returning thread object.")
            threads = [thread for thread in channel.threads if thread.name == title]
            if len(threads) == 1:
                return threads[0]

        return None

    async def create_announcement(
        self,
        announce_channel: TextChannel | None,
        embed: Embed,
        feed_title: str,
        announcement: str,
        message: Thread,
    ) -> None:
        """
        Post an announcement with a truncated Embed object as well as a link to the thread.
        """

        # return if there is no announcement channel
        if not announce_channel:
            return None

        announce_title = f":thread: :clap:\n# {announcement}\n\n# Discuss here!"
        announce_message = f"{announce_title} {message.jump_url}\n\n"

        if isinstance(announce_channel, TextChannel):
            last_message_id = announce_channel.last_message_id
            if last_message_id:
                last_announcement_message = await announce_channel.fetch_message(last_message_id)
                if last_announcement_message:
                    if announce_title in last_announcement_message.content:
                        log.info(
                            f"{feed_title}: Announcement message already exists in channel '{announce_channel.guild.name}/{announce_channel.name}' (id={announce_channel}), skipping."
                        )
                        return

            log.info(
                f"{feed_title}: Sending announcement message to '{announce_channel.guild.name}/{announce_channel.name}' (id={announce_channel.id})"
            )
            _announcement = await announce_channel.send(content=announce_message, embed=embed)
        else:
            log.warning(f"{feed_title}: Configured announcement channel (id={announce_channel}) is not a TextChannel")

    def check_rss(self, rss: RssFeedToChannel, episode_number_override: int | None = None) -> EpisodeData | None:
        """
        If the latest episode is newer than the currently stored episode,
        return new episode
        """
        current_episode = episode_number_override or rss.current_episode

        latest_episode = self._get_latest_episode_data(rss)

        if latest_episode.number > current_episode:
            rss.current_episode = latest_episode.number
            return latest_episode
        return None

    async def _check_rss_feed(self, feed: RssFeedToChannel):
        if (latest_episode := self.check_rss(rss=feed)) is not None:
            log.info(f"{feed.title}: New episode found: {latest_episode.number}")

            title = latest_episode.get_title(feed.title_prefix, feed.override_episode_prepend_title)
            channel_embed = self.get_embed(feed, latest_episode)
            announce_embed = self.get_embed(feed, latest_episode, truncate=True)

            thread = None
            for index, (_announce_channel, _channel) in enumerate(
                feed.get_channels(
                    settings.override_announce_channel_id,
                    settings.override_channel_id,
                )
            ):
                channel = self.bot.get_channel(_channel)
                if not channel:
                    log.info(f"{feed.title}-{index}: Channel (id={_channel}) not found! Skipping.")
                    continue
                log.info(f"{feed.title}-{index}: Found channel (id={_channel}): {channel.guild.name}/{channel.name}")

                announce_channel = self.bot.get_channel(_announce_channel)
                if announce_channel:
                    log.info(
                        f"{feed.title}-{index}: Found channel (id={_announce_channel}): {channel.guild.name}/{announce_channel.name}"
                    )

                if isinstance(channel, TextChannel):
                    thread = await self.add_text_thread(
                        channel=channel,
                        title=title,
                        embed=channel_embed,
                        latest_episode_number=latest_episode.number,
                        feed_title=feed.title,
                        override_episode_check=feed.override_episode_check,
                    )

                elif isinstance(channel, ForumChannel):
                    # If the channel is a Forum, spawn a post (that is actually a thread)
                    thread = await self.add_forum_thread(
                        channel=channel,
                        title=title,
                        embed=channel_embed,
                        latest_episode_number=latest_episode.number,
                        feed_title=feed.title,
                        override_episode_check=feed.override_episode_check,
                    )

                if thread:
                    await self.create_announcement(
                        announce_channel=announce_channel,
                        embed=announce_embed,
                        feed_title=feed.title,
                        announcement=title,
                        message=thread,
                    )

                # Add subscribers belonging to $ROLE to thread
                if (role := feed.subscriber_role_id) is not None:
                    if members := thread.guild.get_role(role):
                        for member in members.members:
                            await thread.add_user(member)

        else:
            log.debug(f'{feed.title}: No updates.')

    def dump_feed(self, feed: RssFeedToChannel):
        iterator = 0
        filename = Path(settings.log_path) / f"{datetime.today().strftime('%Y%m%d')}_error-{iterator}_{feed.title}.json"
        while filename.exists():
            iterator += 1
            filename = (
                Path(settings.log_path) / f"{datetime.today().strftime('%Y%m%d')}_error-{iterator}_{feed.title}.json"
            )
        feed.get_tmp_feed_path(settings.log_path).rename(filename)

    @tasks.loop(minutes=settings.check_interval_min)
    async def check_rss_feed(self):
        """Actual bot loop"""
        log.debug("Checking RSS feed...")
        for feed in self.feeds:
            if feed.error_count > settings.error_count_disable:
                log.warning(
                    f'{feed.title} has exceeded error count, skipping. To clear this counter restart the service.'
                )
                continue
            if feed.enabled is False:
                log.debug(f'{feed.title}: Is disabled, skipping.')
                continue

            try:
                await self._check_rss_feed(feed=feed)
            except MalformedFeedDecodeError as e:
                log.critical(f'{feed.title}: {e}')
                log.error(traceback.format_exc())
                self.dump_feed(feed)
            except Exception as e:
                feed.error_count += 1
                log.critical(f'{feed.title}: {e}')
                log.error(traceback.format_exc())


def setup(bot: Bot):
    rsswatcher = RssWatcher(bot)
    bot.add_cog(rsswatcher)
