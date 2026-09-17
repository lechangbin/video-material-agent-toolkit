"""Managed-runtime worker. It reads one request from stdin and emits one JSON result."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def _allowed_url(platform: str, url: str) -> bool:
    host = (urlsplit(url).hostname or "").casefold()
    if platform == "youtube":
        return host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
    if platform == "tiktok":
        return host == "tiktok.com" or host.endswith(".tiktok.com")
    return False


def _extractor_matches(platform: str, info: dict[str, Any]) -> bool:
    identity = str(info.get("extractor_key") or info.get("extractor") or "").casefold()
    return identity.startswith(platform)


def _base_options(request: dict[str, Any]) -> dict[str, Any]:
    platform = str(request["platform"])
    options: dict[str, Any] = {
        "ignoreconfig": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "proxy": str(request["proxy"]),
        "allowed_extractors": [platform],
        "socket_timeout": int(request.get("timeout_seconds", 30)),
    }
    profile = request.get("browser_profile")
    if profile:
        channel = str(request.get("browser_channel", ""))
        if channel not in {"edge", "chrome"}:
            raise ValueError("browser_channel_invalid")
        options["cookiesfrombrowser"] = (channel, str(profile), None, None)
    return options


def _auth_probe(request: dict[str, Any]) -> dict[str, Any]:
    import yt_dlp  # type: ignore[import-untyped]

    platform = str(request["platform"])
    required_cookie_names = {
        "youtube": {"SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"},
        "tiktok": {"sessionid", "sessionid_ss", "sid_guard"},
    }
    if platform not in required_cookie_names:
        raise ValueError("foreign_platform_unsupported")
    with yt_dlp.YoutubeDL(_base_options(request)) as client:
        cookie_names = {cookie.name for cookie in client.cookiejar}
    return {
        "operation": "auth_probe",
        "platform": platform,
        "authenticated": bool(cookie_names & required_cookie_names[platform]),
    }


def _search(request: dict[str, Any]) -> dict[str, Any]:
    if request["platform"] != "youtube":
        raise ValueError("foreign_platform_unsupported")
    import yt_dlp

    options = _base_options(request)
    options["extract_flat"] = "in_playlist"
    options["skip_download"] = True
    query = f"ytsearch{int(request['limit'])}:{request['query']}"
    with yt_dlp.YoutubeDL(options) as client:
        result = client.extract_info(query, download=False)
    entries = []
    for item in (result or {}).get("entries") or []:
        if not isinstance(item, dict) or not _extractor_matches("youtube", item):
            continue
        url = str(item.get("webpage_url") or item.get("url") or "")
        if not _allowed_url("youtube", url):
            continue
        if item.get("_type") in {"playlist", "channel", "url_transparent"}:
            continue
        entries.append(item)
    return {"operation": "search", "platform": "youtube", "entries": entries}


def _resolve(request: dict[str, Any]) -> dict[str, Any]:
    import yt_dlp

    platform = str(request["platform"])
    url = str(request["url"])
    if not _allowed_url(platform, url):
        raise ValueError("foreign_platform_unsupported")
    options = _base_options(request)
    options["skip_download"] = True
    with yt_dlp.YoutubeDL(options) as client:
        info = client.extract_info(url, download=False)
    if not isinstance(info, dict) or not _extractor_matches(platform, info):
        raise ValueError("foreign_platform_unsupported")
    return {"operation": "resolve", "platform": platform, "info": info}


def _download(request: dict[str, Any]) -> dict[str, Any]:
    import yt_dlp

    platform = str(request["platform"])
    url = str(request["url"])
    if not _allowed_url(platform, url):
        raise ValueError("foreign_platform_unsupported")
    destination = Path(str(request["destination"]))
    destination.parent.mkdir(parents=True, exist_ok=True)
    options = _base_options(request)
    options.update(
        {
            "format": str(request["format_selector"]),
            "outtmpl": str(destination),
            "overwrites": True,
            "continuedl": True,
            "nopart": False,
            "merge_output_format": "mp4",
        }
    )
    with yt_dlp.YoutubeDL(options) as client:
        info = client.extract_info(url, download=True)
    if not isinstance(info, dict) or not _extractor_matches(platform, info):
        destination.unlink(missing_ok=True)
        raise ValueError("foreign_platform_unsupported")
    return {
        "operation": "download",
        "platform": platform,
        "destination": str(destination),
        "info": info,
    }


def main() -> None:
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            raise TypeError("request_invalid")
        operation = request.get("operation")
        if operation == "auth_probe":
            result = _auth_probe(request)
        elif operation == "search":
            result = _search(request)
        elif operation == "resolve":
            result = _resolve(request)
        elif operation == "download":
            result = _download(request)
        else:
            raise ValueError("operation_invalid")
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    except Exception as error:  # noqa: BLE001 - worker must structure upstream errors
        code = str(error) if str(error) in {
            "foreign_platform_unsupported",
            "request_invalid",
            "operation_invalid",
            "browser_channel_invalid",
        } else "yt_dlp_operation_failed"
        print(json.dumps({"ok": False, "error": {"code": code}}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
