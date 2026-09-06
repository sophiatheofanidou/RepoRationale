"""Link-header parsing for GitHub REST pagination."""


def parse_link_header(link_header: str | None) -> dict[str, str]:
    """Parse a GitHub `Link` header into a mapping of `rel` to absolute URL.

    Returns an empty mapping for a missing or empty header. Unrecognized
    segments are ignored rather than raised, since only `rel="next"` is used
    by the adapter today.
    """
    if not link_header:
        return {}

    links: dict[str, str] = {}
    for part in link_header.split(","):
        segments = [segment.strip() for segment in part.split(";")]
        url_segment = segments[0]
        if (
            len(segments) < 2
            or not url_segment.startswith("<")
            or not url_segment.endswith(">")
        ):
            continue
        url = url_segment[1:-1]
        for segment in segments[1:]:
            if segment.startswith("rel="):
                rel = segment[len("rel=") :].strip('"')
                links[rel] = url
    return links
