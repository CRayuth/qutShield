import base64
import binascii
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import instaloader


USERNAME = re.compile(r"[A-Za-z0-9._]{1,30}\Z")
SHORTCODE = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
MEDIA_ID = re.compile(r"[0-9]{1,25}\Z")
# Burp Repeater's Firefox string. Instagram 403s python-httpx and Instaloader's Chrome UA.
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"


@dataclass(frozen=True)
class InstagramLink:
    kind: str
    value: str
    owner: str | None = None


def parse_instagram_url(value: str) -> InstagramLink:
    try:
        parts = urlsplit(value.strip())
        hostname = (parts.hostname or "").lower()
        segments = [segment for segment in parts.path.split("/") if segment]
    except ValueError as error:
        raise ValueError("Send an Instagram profile URL") from error
    if parts.scheme != "https" or hostname not in {"instagram.com", "www.instagram.com"}:
        raise ValueError("Send an https://www.instagram.com/<username> profile URL")
    if parts.port is not None or parts.username or parts.password:
        raise ValueError("Invalid Instagram URL")
    if len(segments) == 1 and USERNAME.fullmatch(segments[0]):
        username = segments[0]
        if username.lower() not in {"p", "reel", "reels", "stories", "explore", "s"}:
            return InstagramLink("profile", username)
    if len(segments) == 2 and segments[0] in {"p", "reel", "reels"} and SHORTCODE.fullmatch(segments[1]):
        return InstagramLink("post", segments[1])
    if len(segments) == 3 and segments[0] == "stories" and USERNAME.fullmatch(segments[1]) and MEDIA_ID.fullmatch(segments[2]):
        return InstagramLink("story", segments[2], segments[1])
    if len(segments) == 2 and segments[0] == "s":
        try:
            decoded = base64.urlsafe_b64decode(segments[1] + "=" * (-len(segments[1]) % 4)).decode("ascii")
        except (ValueError, UnicodeError, binascii.Error):
            raise ValueError("Invalid Instagram highlight URL") from None
        story_id = parse_qs(parts.query).get("story_media_id", [""])[0]
        if decoded.startswith("highlight:") and MEDIA_ID.fullmatch(decoded[10:]) and MEDIA_ID.fullmatch(story_id):
            return InstagramLink("highlight", story_id, decoded[10:])
    raise ValueError("Send an Instagram profile, post, story, or highlight URL")


@dataclass(frozen=True)
class Media:
    key: str
    url: str
    kind: str
    label: str


class InstagramSource:
    """Anonymous instaloader access to posts and reels.

    Do not load the Netscape cookie jar here. A challenged sessionid makes
    Instagram 401 the GraphQL query that public posts otherwise answer.
    Stories and highlights stay on gallery-dl / Nimbus.
    """

    def __init__(self):
        self.loader = instaloader.Instaloader(quiet=True, user_agent=BROWSER_UA)

    def media(self, link: InstagramLink):
        if link.kind == "post":
            post = instaloader.Post.from_shortcode(self.loader.context, link.value)
            yield self.post_media(post)
            return
        if link.kind in {"story", "highlight"}:
            raise ValueError("Instagram stories are fetched with gallery-dl, not instaloader")
        username = link.value
        profile = instaloader.Profile.from_username(self.loader.context, username)
        if profile.is_private:
            raise ValueError("This profile is private, so I can't see its posts 🔒")
        yield [Media("profile-picture", profile.profile_pic_url, "photo", "Profile picture")]
        seen = set()
        singles = []
        for get_posts in (profile.get_posts, profile.get_reels):
            for post in get_posts():
                if post.shortcode in seen:
                    continue
                seen.add(post.shortcode)
                group = self.post_media(post)
                if len(group) == 1:
                    singles.extend(group)
                    if len(singles) == 10:
                        yield singles
                        singles = []
                elif group:
                    if singles:
                        yield singles
                        singles = []
                    yield group
            if singles:
                yield singles
                singles = []

    @staticmethod
    def post_media(post) -> list[Media]:
        label = f"https://www.instagram.com/p/{post.shortcode}/"
        if post.typename == "GraphSidecar":
            group = []
            for index, node in enumerate(post.get_sidecar_nodes(), start=1):
                kind = "video" if node.is_video else "photo"
                url = node.video_url if node.is_video else node.display_url
                if url:
                    group.append(Media(f"{post.shortcode}:{index}", url, kind, label))
            return group
        kind = "video" if post.is_video else "photo"
        url = post.video_url if post.is_video else post.url
        return [Media(post.shortcode, url, kind, label)] if url else []
