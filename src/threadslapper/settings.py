import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Annotated, Any, Iterator, Literal, Tuple

import yaml
from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, SecretStr
from pydantic_extra_types import color
from pydantic_settings import BaseSettings, SettingsConfigDict


def prevalidate_boolean(v: Any) -> bool:
    """Evaluate 'true' strings to True, everything else to False"""
    if v is None:
        return False
    if isinstance(v, str):
        return v.lower() in ['true', 't', 'yes', 'y', '1']
    return v


def validate_string(v: str) -> str:
    """Remove unwanted whitespace and check for quotation marks"""
    v = v.strip()
    assert '"' not in v, "Quotation symbol `\"` detected, please remove."
    assert "'" not in v, "Quotation symbol `\'` detected, please remove."
    return v


def validate_secretstr(v: SecretStr) -> SecretStr:
    """Remove unwanted whitespace and check for quotation marks"""
    _v = v.get_secret_value()
    _v = _v.strip()
    if '"' in _v:
        raise AssertionError("Quotation symbol `\"` detected, please remove.")
    if "'" in _v:
        raise AssertionError("Quotation symbol `\"` detected, please remove.")
    if " # " in _v:
        raise AssertionError("A comment has somehow appeared in this key, please remove.")
    return SecretStr(_v)


def validate_rss_feed(v: str) -> str:
    assert v.strip() != "", "RSS Feed can not be blank!"
    return v


def validate_channel_id(v: int) -> int:
    assert v > 0, "Channel ID must be >0!"
    return v


def validate_color(v: int) -> int:
    return max(min(255, v), 0)


def validate_nonnegative(v: int) -> int:
    return abs(v)


def prevalidate_blank_string(v: Any) -> int | None:
    if isinstance(v, str):
        if v.isnumeric():
            return int(v)
    if isinstance(v, int):
        return v
    return None


def validate_channel_list(v: list[dict[str, int]]) -> list[dict[str, int]]:
    for el in v:
        assert list(el.keys()) == ["channel", "announce_channel"]
    return v


class RssFeedToChannel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    error_count: Annotated[int, AfterValidator(validate_nonnegative)] = 0  # this is set by the script

    enabled: Annotated[bool, BeforeValidator(prevalidate_boolean)] = True
    title_prefix: Annotated[str, AfterValidator(validate_string)] = ""
    title: Annotated[str, AfterValidator(validate_string)] = "default"
    channel_id: Annotated[int, AfterValidator(validate_channel_id)] = -1
    subscriber_role_id: int | None = None
    announce_channel_id: Annotated[int, AfterValidator(validate_channel_id)] = -1
    channel_list: Annotated[list[dict[str, int]], AfterValidator(validate_channel_list)] = []
    rss_feed: Annotated[str, AfterValidator(validate_string), AfterValidator(validate_rss_feed)] = ""
    color_theme_r: Annotated[int, AfterValidator(validate_color)] = 0
    color_theme_g: Annotated[int, AfterValidator(validate_color)] = 0
    color_theme_b: Annotated[int, AfterValidator(validate_color)] = 0

    # keys to pick out of the rss xml document
    rss_episode_key: Annotated[str, AfterValidator(validate_string)] = "itunes_episode"
    rss_episode_url_key: Annotated[str, AfterValidator(validate_string)] = "link"
    rss_title_key: Annotated[str, AfterValidator(validate_string)] = "itunes_title"
    rss_summary_key: Annotated[str, AfterValidator(validate_string)] = "subtitle"
    rss_description_key: Annotated[str, AfterValidator(validate_string)] = "subtitle"
    rss_image_key: Annotated[str, AfterValidator(validate_string)] | None = "image"
    rss_tag_key: Annotated[str, AfterValidator(validate_string)] = "tags"

    rss_channel_title_key: Annotated[str, AfterValidator(validate_string)] = "title"
    rss_channel_url_key: Annotated[str, AfterValidator(validate_string)] = "link"
    rss_channel_image_key: Annotated[str, AfterValidator(validate_string)] = "image"
    rss_channel_last_published_key: Annotated[str, AfterValidator(validate_string)] = "published"

    # Use this for overriding patroen RSS feed GUIDs
    override_episode_numbers: Annotated[bool, BeforeValidator(prevalidate_boolean)] = False
    override_episode_check: Annotated[bool, BeforeValidator(prevalidate_boolean)] = False
    override_episode_prepend_title: Annotated[bool, BeforeValidator(prevalidate_boolean)] = False
    current_episode: int = 0
    rss_feed_is_backwards: Annotated[bool, BeforeValidator(prevalidate_boolean)] = False

    def get_color_theme(self) -> Tuple[int, int, int]:
        return (self.color_theme_r, self.color_theme_g, self.color_theme_b)

    def get_latest_episode_index_position(self) -> int:
        """
        Gets the index where the latest episode is, this is either -1
        (end of the rss feed) or 0 (beginning of the RSS feed)
        """
        if self.rss_feed_is_backwards:
            return -1
        return 0

    def get_channels(
        self,
        override_announce_channel_id: int | None = None,
        override_channel_id: int | None = None,
    ) -> list[Tuple[int, int]]:
        if override_announce_channel_id or override_channel_id:
            return [(override_announce_channel_id or -1, override_channel_id or -1)]
        elif self.channel_list:
            return [(channel.get("announce_channel", -1), channel.get("channel", -1)) for channel in self.channel_list]
        else:
            return [(self.announce_channel_id, self.channel_id)]

    def get_tmp_feed_path(self, logging_path: str) -> Path:
        iterator = 0
        path = Path(logging_path) / f"tmp_{self.title}_curep{self.current_episode}_{iterator}.json"
        # while path.exists():
        #     iterator += 1
        #     path = Path(logging_path) / f"tmp_{self.title}_curep{self.current_episode}_{iterator}.json"
        return path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="threadslapper_",
        env_nested_delimiter="__",
        env_file=".env",
    )

    token: Annotated[SecretStr, AfterValidator(validate_secretstr)] = SecretStr("foo")
    check_interval_min: int = 5
    log_path: Annotated[str, AfterValidator(validate_string)] = "."
    config_path: Annotated[str, AfterValidator(validate_string)] = "config/"
    config_file: Annotated[str, AfterValidator(validate_string)] = "example_config.yml"
    startup_latest_episode_check: bool = True  # check for latest episodes on power on

    # how many errors does it take to disable an individiual feed?
    error_count_disable: Annotated[int, AfterValidator(validate_nonnegative)] = 3

    # A single RSS feed can be defined, or a list of yaml objects
    channel: RssFeedToChannel | None = None
    channels_file: str | None = None

    override_announce_channel_id: Annotated[
        Annotated[int, AfterValidator(validate_channel_id)] | None, BeforeValidator(prevalidate_blank_string)
    ] = None
    override_channel_id: Annotated[
        Annotated[int, AfterValidator(validate_channel_id)] | None, BeforeValidator(prevalidate_blank_string)
    ] = None

    debug: Annotated[bool, BeforeValidator(prevalidate_boolean)] = False

    def create_logger(self, name: str) -> logging.Logger:
        log = logging.getLogger(name)
        log.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            '%(asctime)s - [%(levelname)s] [ln: %(lineno)d] (%(process)d) - %(message)s',
            "%Y-%m-%d %H:%M:%S %z",
        )
        stdout = logging.StreamHandler(sys.stdout)
        stdout.setLevel(logging.INFO)
        stdout.setFormatter(formatter)
        file = TimedRotatingFileHandler(
            filename=os.path.join(self.log_path, f'discordbot.log'),
            when='W0',
            backupCount=10,
        )
        file.setLevel(logging.INFO)
        file.setFormatter(formatter)
        fileDebug = TimedRotatingFileHandler(
            filename=os.path.join(self.log_path, f'discordbot_debug.log'),
            when='W0',
            backupCount=10,
        )
        fileDebug.setLevel(logging.DEBUG)
        fileDebug.setFormatter(formatter)
        fileError = TimedRotatingFileHandler(
            filename=os.path.join(self.log_path, f'discordbot_errors.log'),
            when='W0',
            backupCount=10,
        )
        fileError.setLevel(logging.WARNING)
        fileError.setFormatter(formatter)
        log.addHandler(stdout)
        log.addHandler(file)
        log.addHandler(fileDebug)
        log.addHandler(fileError)

        return log

    def get_channels_list(self) -> list[RssFeedToChannel]:
        """
        Parses the config yaml file.
        """
        obj = {}
        config_file = os.path.join(self.config_path, self.config_file)
        if os.path.exists(config_file):
            with open(config_file, mode='r') as f:
                obj = yaml.safe_load(f)

        feeds: list[RssFeedToChannel] = []
        try:
            if self.channel:
                feeds.append(self.channel)

            for key, value in obj.items():
                rss = RssFeedToChannel(
                    enabled=value.get('enabled', True),
                    title=key,
                    title_prefix=value.get('title_prefix', ''),
                    subscriber_role_id=value.get('subscriber_role_id', None),
                    rss_feed=value.get('rss_url', ''),
                    override_episode_numbers=value.get('override_episode_numbers', False),
                    override_episode_check=value.get('override_episode_check', False),
                    override_episode_prepend_title=value.get('override_episode_prepend_title', False),
                )

                channel = value.get('channel_id', -1)
                announce_channel = value.get('announce_channel_id', None)
                rss.announce_channel_id = announce_channel

                if isinstance(channel, int):
                    rss.channel_id = channel
                elif isinstance(channel, list):
                    rss.hybrid_channel_list = [
                        (pair.get('channel', None), pair.get('announce_channel', None)) for pair in channel
                    ]

                if (rss_key := value.get('rss_episode_key', None)) is not None:
                    rss.rss_episode_key = rss_key
                if (rss_key := value.get('rss_title_key', None)) is not None:
                    rss.rss_title_key = rss_key
                if (rss_key := value.get('rss_description_key', None)) is not None:
                    rss.rss_description_key = rss_key
                if (rss_key := value.get('rss_image_key', None)) is not None:
                    rss.rss_image_key = rss_key
                if (rss_key := value.get('rss_tag_key', None)) is not None:
                    rss.rss_tag_key = rss_key

                if (rss_key := value.get('rss_channel_title_key', None)) is not None:
                    rss.rss_channel_title_key = rss_key
                if (rss_key := value.get('rss_channel_url_key', None)) is not None:
                    rss.rss_channel_url_key = rss_key
                if (rss_key := value.get('rss_channel_image_key', None)) is not None:
                    rss.rss_channel_image_key = rss_key
                if (rss_key := value.get('rss_channel_last_published_key', None)) is not None:
                    rss.rss_channel_last_published_key = rss_key

                if (rss_key := value.get('color_theme_r', None)) is not None:
                    rss.color_theme_r = rss_key
                if (rss_key := value.get('color_theme_g', None)) is not None:
                    rss.color_theme_g = rss_key
                if (rss_key := value.get('color_theme_b', None)) is not None:
                    rss.color_theme_b = rss_key

                if (rss_key := value.get('rss_feed_is_backwards', None)) is not None:
                    rss.rss_feed_is_backwards = rss_key

                if (rss_key := value.get('channel_list', None)) is not None:
                    rss.channel_list = rss_key

                feeds.append(rss)
        except Exception as e:
            logging.getLogger(__name__).error(e)

        return feeds
