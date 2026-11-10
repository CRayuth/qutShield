"""Add Facebook Story support to the installed yt-dlp (upstream issue #5763).

Official yt-dlp still has no Facebook Story extractor. The community fork that
adds it (0xvd/yt-dlp-exp@fix/facebook_stories) is ~6 months behind current
yt-dlp, so installing it would regress every other extractor. Instead we apply
only its three small changes on top of the pinned, current package.

Every replacement must match exactly once or the build aborts, so a future
yt-dlp bump fails loudly rather than silently shipping a broken patch.

Usage: python patches/apply_ytdlp_facebook_stories.py
"""
import importlib.util
import py_compile
import re
import sys
from pathlib import Path

# (label, exact text to find, replacement)
EDITS = [
    (
        "_VALID_URL accepts /stories/",
        "                            watchparty/\n                        )|\n",
        "                            watchparty/|\n                            stories/\n                        )|\n",
    ),
    (
        "relay prefetched data searches 'bucket'",
        "                target_keys=('video', 'event', 'nodes', 'node', 'mediaset'))\n",
        "                target_keys=('video', 'event', 'nodes', 'node', 'mediaset', 'bucket'))\n",
    ),
    (
        "story edges traversed and their attachments parsed",
        "                edges = try_get(data, lambda x: x['mediaset']['currMedia']['edges'], list) or []\n"
        "                for edge in edges:\n"
        "                    parse_attachment(edge, key='node')\n",
        "                edges = traverse_obj(data,\n"
        "                                     ('mediaset', 'currMedia', 'edges', {list}),\n"
        "                                     ('bucket', 'unified_stories_with_notes', 'edges', {list}), default=[])\n"
        "                story_card_id = self._search_regex(\n"
        "                    r'/stories/[^/]+/([^/?]+)', url, 'story card id', default=None)\n"
        "                for edge in edges:\n"
        "                    if story_card_id and traverse_obj(edge, ('node', 'id')) != story_card_id:\n"
        "                        continue\n"
        "                    for node in traverse_obj(edge, ('node', 'attachments'), default=[]):\n"
        "                        parse_attachment(node)\n"
        "                    parse_attachment(edge, key='node')\n",
    ),
    (
        "photo story attachments",
        "                    if media.get('__typename') == 'Video':\n                        return parse_graphql_video(media)\n",
        "                    if media.get('__typename') == 'Video':\n                        return parse_graphql_video(media)\n"
        "                    if media.get('__typename') == 'Photo':\n"
        "                        image = traverse_obj(media, ('image', 'uri'), ('photo_image', 'uri'), {url_or_none})\n"
        "                        if image:\n"
        "                            entries.append({'id': media.get('id') or video_id, 'url': image, 'ext': 'jpg',\n"
        "                                           'title': f'Facebook photo #{media.get(\"id\") or video_id}'})\n",
    ),
]

STORY_URL = "https://www.facebook.com/stories/122107619162285573/UzpfSVNDOjI5MTc0ODgzMDcyMTQxMjkx/"


def facebook_module_path() -> Path:
    spec = importlib.util.find_spec("yt_dlp.extractor.facebook")
    if spec is None or not spec.origin:
        raise SystemExit("yt_dlp.extractor.facebook not importable")
    return Path(spec.origin)


def _drop_lazy_extractors(facebook_path: Path) -> None:
    """yt-dlp matches URLs from lazy_extractors.py, not facebook.py.

    Patching FacebookIE._VALID_URL is a no-op until this generated table is
    gone; otherwise /stories/ is still Unsupported URL.
    """
    lazy = facebook_path.parent / "lazy_extractors.py"
    if lazy.is_file():
        lazy.unlink()
        print("removed %s" % lazy.name)


def main() -> int:
    path = facebook_module_path()
    source = path.read_text(encoding="utf-8")
    changed = False

    for label, find, replace in EDITS:
        if find not in source:
            if replace in source:
                print("already: %s" % label)
                continue
            print("PATCH ABORT: anchor %r matched 0 times, expected exactly 1." % label)
            print("  yt-dlp has changed this code; update patches/apply_ytdlp_facebook_stories.py.")
            return 1
        hits = source.count(find)
        if hits != 1:
            print("PATCH ABORT: anchor %r matched %d times, expected exactly 1." % (label, hits))
            print("  yt-dlp has changed this code; update patches/apply_ytdlp_facebook_stories.py.")
            return 1
        source = source.replace(find, replace, 1)
        changed = True
        print("applied: %s" % label)

    if changed:
        path.write_text(source, encoding="utf-8")
        py_compile.compile(str(path), doraise=True)
        print("wrote and byte-compiled %s" % path)

    _drop_lazy_extractors(path)

    # Re-import from scratch and confirm a Story URL is now recognised.
    for name in [key for key in sys.modules if key.startswith("yt_dlp")]:
        del sys.modules[name]
    from yt_dlp.extractor.facebook import FacebookIE
    from yt_dlp.extractor import gen_extractor_classes

    if not re.match(FacebookIE._VALID_URL, STORY_URL):
        print("PATCH VERIFY FAILED: FacebookIE still rejects the Story URL")
        return 1
    if not any(cls.suitable(STORY_URL) and getattr(cls, "IE_NAME", "") != "generic" for cls in gen_extractor_classes()):
        print("PATCH VERIFY FAILED: no extractor claims a /stories/ URL (lazy table still hiding FacebookIE)")
        return 1
    print("verified: FacebookIE._VALID_URL now matches a /stories/ URL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
