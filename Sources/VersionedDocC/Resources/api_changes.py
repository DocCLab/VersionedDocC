#!/usr/bin/env python3

import argparse
import difflib
import hashlib
import html
import json
from pathlib import Path


CHANGE_ORDER = {"added": 0, "modified": 1, "removed": 2}
SOURCE_ORDER = {"api": 0, "article": 1}
ARTICLE_CONTENT_FIELDS = (
    "abstract",
    "primaryContentSections",
    "sections",
    "topicSections",
)
ARTICLE_DIFF_LINE_LIMIT = 160


def positive_integer(value):
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_arguments():
    parser = argparse.ArgumentParser(description="Generate a versioned documentation changes site")
    parser.add_argument("--symbol-graph", action="append", default=[])
    parser.add_argument("--article-root", action="append", default=[])
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--hosting-base-path", required=True)
    parser.add_argument("--default-version", required=True)
    parser.add_argument("--build-date", required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--module-path", required=True)
    parser.add_argument(
        "--module",
        action="append",
        default=[],
        help="Configured module in MODULE_NAME=MODULE_PATH form",
    )
    parser.add_argument("--page-size", type=positive_integer, default=10)
    parser.add_argument("--star-repository-url")
    parser.add_argument("--powered-by-url")
    return parser.parse_args()


def parse_versioned_paths(specifications):
    versions = []
    paths = {}
    for specification in specifications:
        version, separator, raw_path = specification.partition("=")
        if not separator or not version or not raw_path:
            raise ValueError(f"invalid versioned path specification: {specification}")
        if version not in paths:
            versions.append(version)
            paths[version] = []
        paths[version].append(Path(raw_path))
    return versions, paths


def parse_modules(specifications, primary_module_path):
    modules = []
    names = set()
    paths = set()
    for index, specification in enumerate(specifications):
        name, separator, path = specification.partition("=")
        if not separator or not name or not path:
            raise ValueError(f"invalid module specification: {specification}")
        normalized_name = name.casefold()
        normalized_path = path.casefold()
        if normalized_name in names:
            raise ValueError(f"duplicate module name: {name}")
        if normalized_path in paths:
            raise ValueError(f"duplicate module path: {path}")
        modules.append({"name": name, "path": path, "primary": index == 0})
        names.add(normalized_name)
        paths.add(normalized_path)
    if not modules:
        modules.append(
            {
                "name": primary_module_path,
                "path": primary_module_path,
                "primary": True,
            }
        )
    elif modules[0]["path"].casefold() != primary_module_path.casefold():
        raise ValueError(
            "the first configured module path must match --module-path: "
            f"{modules[0]['path']} != {primary_module_path}"
        )
    return modules


def graph_module(graph, modules):
    configured_by_name = {module["name"].casefold(): module for module in modules}
    raw_name = graph.get("module", {}).get("name")
    if isinstance(raw_name, str):
        configured = configured_by_name.get(raw_name.casefold())
        if configured is not None:
            return configured
    primary = next((module for module in modules if module.get("primary")), modules[0])
    if len(modules) == 1 or not isinstance(raw_name, str):
        return primary
    raise ValueError(
        f"symbol graph module {raw_name!r} is not configured; expected one of "
        + ", ".join(module["name"] for module in modules)
    )


def format_version(value):
    if not isinstance(value, dict):
        return str(value)
    components = [value.get("major"), value.get("minor"), value.get("patch")]
    while len(components) > 2 and components[-1] in (None, 0):
        components.pop()
    while components and components[-1] is None:
        components.pop()
    return ".".join(str(component) for component in components)


def availability_labels(symbol):
    labels = []
    for item in symbol.get("availability", []):
        domain = item.get("domain", "Platform")
        if item.get("introduced"):
            labels.append(f"{domain} {format_version(item['introduced'])}")
        elif item.get("deprecated"):
            labels.append(f"{domain} deprecated {format_version(item['deprecated'])}")
    return labels[:6]


def load_docc_urls(documentation_root):
    urls = {}
    if not documentation_root.is_dir():
        return urls
    for path in documentation_root.rglob("*.json"):
        with path.open(encoding="utf-8") as source:
            document = json.load(source)
        precise = document.get("metadata", {}).get("externalID")
        identifier = document.get("identifier", {}).get("url")
        reference = document.get("references", {}).get(identifier, {})
        url = reference.get("url")
        if not precise or not isinstance(url, str):
            continue
        existing = urls.get(precise)
        if existing is not None and existing != url:
            raise ValueError(
                f"conflicting DocC URLs for {precise}: {existing}, {url}"
            )
        urls[precise] = url
    return urls


def load_snapshot(path, module_path, documentation_urls=None, modules=None):
    with path.open(encoding="utf-8") as source:
        graph = json.load(source)
    modules = modules or [
        {"name": module_path, "path": module_path, "primary": True}
    ]
    module = graph_module(graph, modules)
    snapshot = {}
    for raw in graph.get("symbols", []):
        components = raw.get("pathComponents", [])
        if not components:
            continue
        identifier = ".".join(components)
        if identifier in snapshot:
            identifier = f"{identifier}#{raw['identifier']['precise']}"
        declaration = "".join(
            fragment.get("spelling", "")
            for fragment in raw.get("declarationFragments", [])
        )
        precise = raw["identifier"]["precise"]
        if documentation_urls is None:
            suffix = "/".join(component.lower() for component in components)
            documentation_path = f"/documentation/{module['path']}/{suffix}"
        else:
            documentation_path = documentation_urls.get(precise)
        snapshot[identifier] = {
            "id": identifier,
            "precise": precise,
            "title": raw.get("names", {}).get("title", components[-1]),
            "kind": raw.get("kind", {}).get("displayName", "Symbol"),
            "declaration": declaration,
            "pathComponents": components,
            "displayId": ".".join(components),
            "path": documentation_path,
            "availability": availability_labels(raw),
            "moduleName": module["name"],
            "modulePath": module["path"],
        }
    return snapshot


def merge_snapshots(paths, module_path, documentation_urls=None, modules=None):
    """Merge platform-specific graphs, keeping the first graph as primary."""
    modules = modules or [
        {"name": module_path, "path": module_path, "primary": True}
    ]
    namespaced = len(modules) > 1
    merged = {}
    identifiers_by_precise = {}
    for path in paths:
        snapshot = load_snapshot(path, module_path, documentation_urls, modules)
        for identifier, item in snapshot.items():
            namespace = item["modulePath"].casefold()
            precise_key = (namespace, item["precise"])
            existing_identifier = identifiers_by_precise.get(precise_key)
            if existing_identifier is not None:
                existing = merged[existing_identifier]
                existing["availability"] = list(
                    dict.fromkeys(existing["availability"] + item["availability"])
                )
                if existing.get("path") is None and item.get("path") is not None:
                    existing["path"] = item["path"]
                continue
            if namespaced:
                identifier = f"{namespace}:{identifier}"
            if identifier in merged:
                identifier = f"{identifier}#{item['precise']}"
                item["id"] = identifier
            merged[identifier] = item
            identifiers_by_precise[precise_key] = identifier
    return merged


def compare(previous_version, current_version, previous, current, source="api"):
    changes = []
    previous_ids = set(previous)
    current_ids = set(current)
    for identifier in current_ids - previous_ids:
        changes.append(
            {"type": "added", "source": source, "current": current[identifier]}
        )
    for identifier in previous_ids - current_ids:
        changes.append(
            {"type": "removed", "source": source, "previous": previous[identifier]}
        )
    for identifier in previous_ids & current_ids:
        before = previous[identifier]
        after = current[identifier]
        fields = [
            field
            for field in ("declaration", "kind", "availability")
            if before.get(field) != after.get(field)
        ]
        if fields:
            changes.append(
                {
                    "type": "modified",
                    "source": source,
                    "fields": fields,
                    "previous": before,
                    "current": after,
                }
            )
    changes.sort(
        key=lambda item: (
            CHANGE_ORDER[item["type"]],
            (item.get("current") or item["previous"])["displayId"].lower(),
        )
    )
    counts = {
        change_type: sum(change["type"] == change_type for change in changes)
        for change_type in CHANGE_ORDER
    }
    return {
        "id": f"{previous_version}-to-{current_version}",
        "previousVersion": previous_version,
        "currentVersion": current_version,
        "counts": counts,
        "changes": changes,
    }


def inline_text(value):
    if isinstance(value, list):
        return "".join(inline_text(item) for item in value)
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("text"), str):
        return value["text"]
    if isinstance(value.get("code"), str):
        return value["code"]
    for key in ("inlineContent", "titleInlineContent", "overridingTitleInlineContent"):
        if isinstance(value.get(key), list):
            return inline_text(value[key])
    for key in ("overridingTitle", "title", "alt"):
        if isinstance(value.get(key), str):
            return value[key]
    identifier = value.get("identifier")
    if isinstance(identifier, str):
        return identifier.rsplit("/", 1)[-1]
    return ""


def content_lines(value):
    if isinstance(value, list):
        lines = []
        for item in value:
            lines.extend(content_lines(item))
        return lines
    if not isinstance(value, dict):
        return []

    kind = value.get("type")
    if kind == "heading":
        return [f"{'#' * max(1, value.get('level', 1))} {value.get('text', '')}".rstrip()]
    if kind == "paragraph":
        text = inline_text(value.get("inlineContent", []))
        return [text] if text else []
    if kind == "codeListing":
        code = value.get("code", [])
        if isinstance(code, str):
            code = code.splitlines()
        language = value.get("syntax", "")
        return [f"```{language}", *code, "```"]
    if kind == "aside":
        title = value.get("name") or value.get("style") or "Aside"
        return [f"> {title}", *content_lines(value.get("content", []))]
    if kind in ("orderedList", "unorderedList"):
        lines = []
        for index, item in enumerate(value.get("items", []), 1):
            item_lines = content_lines(item.get("content", item))
            if item_lines:
                marker = f"{index}." if kind == "orderedList" else "-"
                lines.append(f"{marker} {item_lines[0]}")
                lines.extend(f"  {line}" for line in item_lines[1:])
        return lines
    if kind == "table":
        lines = []
        for row in value.get("rows", []):
            cells = [" ".join(content_lines(cell)) for cell in row]
            lines.append(" | ".join(cells))
        return lines
    if kind == "termList":
        lines = []
        for item in value.get("items", []):
            term = inline_text(item.get("term", {}))
            definition = content_lines(item.get("definition", {}))
            lines.append(f"{term}: {definition[0] if definition else ''}".rstrip())
            lines.extend(f"  {line}" for line in definition[1:])
        return lines
    if kind in ("text", "codeVoice", "emphasis", "strong", "reference", "link", "image", "topic"):
        text = inline_text(value)
        return [text] if text else []

    lines = []
    title = value.get("title")
    if isinstance(title, str):
        lines.append(f"## {title}")
    identifiers = value.get("identifiers")
    if isinstance(identifiers, list):
        lines.extend(f"- {identifier.rsplit('/', 1)[-1]}" for identifier in identifiers)
    for key in ("content", "items", "definition", "term"):
        if key in value:
            lines.extend(content_lines(value[key]))
    return lines


def article_payload(document):
    metadata = document.get("metadata", {})
    payload = {
        "metadata": {
            "title": metadata.get("title"),
            "role": metadata.get("role"),
        }
    }
    payload.update({field: document.get(field) for field in ARTICLE_CONTENT_FIELDS})
    return payload


def module_for_documentation_path(route, modules):
    marker = "/documentation/"
    if marker not in route:
        return None
    module_path = route.split(marker, 1)[1].split("/", 1)[0]
    return next(
        (
            module
            for module in modules
            if module["path"].casefold() == module_path.casefold()
        ),
        None,
    )


def article_snapshot(documentation_root, modules=None):
    snapshot = {}
    if not documentation_root.is_dir():
        return snapshot
    for path in documentation_root.rglob("*.json"):
        with path.open(encoding="utf-8") as source:
            document = json.load(source)
        if document.get("kind") != "article":
            continue
        identifier = document.get("identifier", {}).get("url")
        if not isinstance(identifier, str):
            continue
        metadata = document.get("metadata", {})
        payload = article_payload(document)
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        route = "/" + path.relative_to(documentation_root.parent).with_suffix("").as_posix()
        module = module_for_documentation_path(route, modules or [])
        lines = [f"# {metadata.get('title', path.stem)}"]
        for field in ARTICLE_CONTENT_FIELDS:
            lines.extend(content_lines(document.get(field, [])))
        snapshot[identifier] = {
            "id": identifier,
            "title": metadata.get("title", path.stem),
            "kind": "Collection" if metadata.get("role") == "collection" else "Article",
            "displayId": route,
            "path": route,
            "declaration": "",
            "availability": [],
            "_digest": hashlib.sha256(serialized.encode()).hexdigest(),
            "_lines": lines,
        }
        if module is not None:
            snapshot[identifier]["moduleName"] = module["name"]
            snapshot[identifier]["modulePath"] = module["path"]
    return snapshot


def public_article(item):
    return {key: value for key, value in item.items() if not key.startswith("_")}


def compare_articles(previous_version, current_version, previous, current):
    changes = []
    previous_ids = set(previous)
    current_ids = set(current)
    for identifier in current_ids - previous_ids:
        item = public_article(current[identifier])
        item["preview"] = "\n".join(current[identifier]["_lines"][:6])
        changes.append({"type": "added", "source": "article", "current": item})
    for identifier in previous_ids - current_ids:
        item = public_article(previous[identifier])
        item["preview"] = "\n".join(previous[identifier]["_lines"][:6])
        changes.append({"type": "removed", "source": "article", "previous": item})
    for identifier in previous_ids & current_ids:
        before = previous[identifier]
        after = current[identifier]
        fields = []
        if before["title"] != after["title"]:
            fields.append("title")
        if before["kind"] != after["kind"]:
            fields.append("role")
        if before["_digest"] != after["_digest"]:
            fields.append("content")
        if not fields:
            continue
        diff = list(
            difflib.unified_diff(
                before["_lines"],
                after["_lines"],
                fromfile=f"Previous · {previous_version}",
                tofile=f"Current · {current_version}",
                n=2,
                lineterm="",
            )
        )
        truncated = len(diff) > ARTICLE_DIFF_LINE_LIMIT
        if truncated:
            diff = diff[:ARTICLE_DIFF_LINE_LIMIT]
        if not diff:
            diff = ["Article structure or formatting changed."]
        changes.append(
            {
                "type": "modified",
                "source": "article",
                "fields": fields,
                "previous": public_article(before),
                "current": public_article(after),
                "diff": diff,
                "diffTruncated": truncated,
            }
        )
    changes.sort(
        key=lambda item: (
            CHANGE_ORDER[item["type"]],
            (item.get("current") or item["previous"])["displayId"].lower(),
        )
    )
    counts = {
        change_type: sum(change["type"] == change_type for change in changes)
        for change_type in CHANGE_ORDER
    }
    return {
        "id": f"{previous_version}-to-{current_version}",
        "previousVersion": previous_version,
        "currentVersion": current_version,
        "counts": counts,
        "changes": changes,
    }


def merge_comparisons(comparisons):
    if not comparisons:
        raise ValueError("at least one comparison source is required")
    merged = {
        key: comparisons[0][key]
        for key in ("id", "previousVersion", "currentVersion")
    }
    for comparison in comparisons[1:]:
        for key in ("previousVersion", "currentVersion"):
            if comparison[key] != merged[key]:
                raise ValueError("comparison versions do not match")
    merged["changes"] = [
        change for comparison in comparisons for change in comparison["changes"]
    ]
    merged["changes"].sort(
        key=lambda item: (
            CHANGE_ORDER[item["type"]],
            SOURCE_ORDER[item["source"]],
            (item.get("current") or item["previous"])["displayId"].lower(),
        )
    )
    merged["counts"] = {
        change_type: sum(
            change["type"] == change_type for change in merged["changes"]
        )
        for change_type in CHANGE_ORDER
    }
    return merged


def legacy_api_comparison(comparison):
    changes = []
    for change in comparison["changes"]:
        if change["source"] != "api":
            continue
        legacy_change = dict(change)
        legacy_change.pop("source")
        changes.append(legacy_change)
    return {
        "id": comparison["id"],
        "previousVersion": comparison["previousVersion"],
        "currentVersion": comparison["currentVersion"],
        "counts": {
            change_type: sum(change["type"] == change_type for change in changes)
            for change_type in CHANGE_ORDER
        },
        "changes": changes,
    }


def render_dashboard(
    comparisons,
    arguments,
    sources=None,
    modules=None,
    selected_module_path=None,
):
    sources = sources or ["api"]
    modules = modules or [
        {
            "name": arguments.project_name,
            "path": arguments.module_path,
            "primary": True,
        }
    ]
    selected_module = next(
        (
            module
            for module in modules
            if module["path"].casefold()
            == (selected_module_path or "").casefold()
        ),
        None,
    )
    if selected_module_path is not None and selected_module is None:
        raise ValueError(f"unknown selected module path: {selected_module_path}")
    multiple_modules = len(modules) > 1
    base = arguments.hosting_base_path.rstrip("/")
    data = json.dumps(
        {
            "comparisons": comparisons,
            "sources": sources,
            "modules": modules,
        },
        separators=(",", ":"),
    ).replace("</", "<\\/")
    comparison_options = "\n".join(
        f'<option value="{html.escape(item["id"])}">'
        f'{html.escape(item["previousVersion"])} → {html.escape(item["currentVersion"])}</option>'
        for item in comparisons
    )
    if selected_module is not None:
        docs_home = (
            f"{base}/{arguments.default_version}/documentation/"
            f"{selected_module['path']}/"
        )
        canonical = (
            f"{base}/{arguments.default_version}/changes/"
            f"{selected_module['path']}/"
        )
    else:
        docs_home = (
            f"{base}/{arguments.default_version}/documentation/"
            if multiple_modules
            else f"{base}/{arguments.default_version}/documentation/{arguments.module_path}/"
        )
        canonical = f"{base}/{arguments.default_version}/changes/"
    changes_title = "API Changes" if sources == ["api"] else "Changes"
    document_title = (
        f"{changes_title} · {selected_module['name']}"
        if selected_module is not None
        else changes_title
    )
    module_options = '<option value="">All Modules</option>' + "".join(
        f'<option value="{html.escape(module["path"])}"'
        f'{" selected" if selected_module is module else ""}>'
        f'{html.escape(module["name"])}</option>'
        for module in modules
    )
    module_control_class = "module-control" + (
        " single-module" if not multiple_modules else ""
    )
    source_options = '<option value="all">All content</option>' + "".join(
        f'<option value="{source}">{"Public API" if source == "api" else "Articles"}</option>'
        for source in sources
    )
    source_control_class = "source-control" + (" single-source" if len(sources) == 1 else "")
    external_attributes = 'target="_blank" rel="noopener noreferrer"'
    star_repository_url = getattr(arguments, "star_repository_url", None)
    powered_by_url = getattr(arguments, "powered_by_url", None)
    star_link = (
        f'<a class="star-link" href="{html.escape(star_repository_url, quote=True)}" '
        f'{external_attributes} aria-label="Star {html.escape(arguments.project_name)} on GitHub">'
        '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.64 0 8.13c0 3.59 2.29 6.64 5.47 7.71.4.08.55-.18.55-.39 0-.19-.01-.83-.01-1.51-2.01.38-2.53-.5-2.69-.96-.09-.23-.48-.96-.82-1.15-.28-.15-.68-.52-.01-.53.63-.01 1.08.59 1.23.83.72 1.23 1.87.88 2.33.67.07-.53.28-.88.51-1.08-1.78-.21-3.64-.91-3.64-4.02 0-.89.31-1.62.82-2.19-.08-.2-.36-1.04.08-2.16 0 0 .67-.22 2.2.84A7.5 7.5 0 0 1 8 3.94a7.5 7.5 0 0 1 2 .27c1.53-1.06 2.2-.84 2.2-.84.44 1.12.16 1.96.08 2.16.51.57.82 1.3.82 2.19 0 3.12-1.87 3.81-3.65 4.02.29.25.54.74.54 1.5 0 1.08-.01 1.95-.01 2.22 0 .22.15.48.55.39A8.14 8.14 0 0 0 16 8.13C16 3.64 12.42 0 8 0Z"/></svg>'
        '<span>Star on GitHub</span></a>'
        if star_repository_url
        else ""
    )
    powered_by = (
        f'<footer class="site-footer"><a href="{html.escape(powered_by_url, quote=True)}" '
        f'{external_attributes}>Powered by VersionedDocC</a></footer>'
        if powered_by_url
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="canonical" href="{html.escape(canonical)}">
  <title>{html.escape(document_title)} | {html.escape(arguments.project_name)} Documentation</title>
  <style>
    :root {{ color-scheme: light dark; --page:#fff; --surface:#f5f5f7; --line:#d2d2d7; --text:#1d1d1f; --secondary:#6e6e73; --link:#06c; --added:#248a3d; --modified:#b65d00; --removed:#c72535; }}
    * {{ box-sizing:border-box; }} [hidden] {{ display:none !important; }} body {{ background:var(--page); color:var(--text); font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif; margin:0; }} a {{ color:var(--link); }}
    .site-header {{ align-items:center; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; min-height:52px; padding:8px max(24px,calc((100vw - 980px)/2)); }}
    .brand {{ color:inherit; font-weight:650; text-decoration:none; }} .brand span,.header-meta,.eyebrow,.comparison-note,.symbol-path,.availability {{ color:var(--secondary); }} .brand span {{ font-weight:400; margin-left:7px; }} .header-actions,.star-link {{ align-items:center; display:flex; }} .header-actions {{ gap:12px; }} .star-link {{ border:1px solid var(--line); border-radius:6px; color:var(--text); font-size:12px; font-weight:500; gap:5px; padding:4px 8px; text-decoration:none; }} .star-link:hover {{ background:var(--surface); }} .star-link svg {{ fill:currentColor; height:14px; width:14px; }} .header-meta,.eyebrow,.symbol-path,.availability {{ font-size:12px; }}
    main {{ margin:0 auto; max-width:1080px; padding:48px 24px 80px; }}
    .controls,.comparison-heading {{ align-items:end; display:flex; gap:16px; justify-content:space-between; }} .controls {{ border-bottom:1px solid var(--line); margin-bottom:36px; padding-bottom:24px; }} .control-group {{ display:flex; gap:12px; }} label {{ color:var(--secondary); display:grid; font-size:12px; gap:5px; }} .single-source,.single-module {{ display:none; }} select,input {{ background:var(--page); border:1px solid var(--line); border-radius:7px; color:var(--text); font:inherit; min-height:36px; padding:5px 10px; }} input {{ min-width:240px; }}
    .eyebrow {{ font-weight:600; letter-spacing:.06em; margin:0 0 4px; text-transform:uppercase; }} h1 {{ font-size:34px; letter-spacing:-.025em; margin:0; }} h1 span {{ color:var(--secondary); font-weight:400; }}
    .summary-grid {{ display:grid; gap:12px; grid-template-columns:repeat(3,1fr); margin:24px 0 28px; }} .summary {{ background:var(--surface); border-radius:12px; display:grid; padding:16px 18px; }} .summary strong {{ font-size:26px; line-height:1.1; }} .summary span {{ color:var(--secondary); font-size:13px; }} .added strong {{ color:var(--added); }} .modified strong {{ color:var(--modified); }} .removed strong {{ color:var(--removed); }}
    .result-bar,.pager {{ align-items:center; display:flex; justify-content:space-between; }} .result-bar {{ color:var(--secondary); margin:0 0 12px; }} .pager {{ gap:8px; }} button {{ background:var(--page); border:1px solid var(--line); border-radius:7px; color:var(--text); font:inherit; padding:7px 12px; }} button:disabled {{ opacity:.45; }}
    .changes-list {{ display:grid; gap:16px; }} .change-card {{ border:1px solid var(--line); border-left:4px solid; border-radius:10px; padding:18px 20px; }} .change-card.added {{ border-left-color:var(--added); }} .change-card.modified {{ border-left-color:var(--modified); }} .change-card.removed {{ border-left-color:var(--removed); }}
    .badge {{ border-radius:999px; color:#fff; font-size:11px; font-weight:650; padding:2px 8px; }} .added .badge {{ background:var(--added); }} .modified .badge {{ background:var(--modified); }} .removed .badge {{ background:var(--removed); }} .kind {{ color:var(--secondary); font-size:12px; margin-left:8px; }} .change-module {{ background:var(--surface); border-radius:999px; color:var(--secondary); float:right; font-size:11px; font-weight:600; padding:2px 8px; }} h2 {{ font-size:19px; margin:10px 0 0; }} h2 a {{ color:inherit; text-decoration:none; }} .symbol-path,.availability {{ margin:2px 0; }} .symbol-path {{ font-family:ui-monospace,"SF Mono",monospace; }}
    .declaration {{ background:var(--surface); border-radius:8px; display:grid; gap:5px; margin-top:13px; padding:11px 13px; }} .declaration span {{ color:var(--secondary); font-size:11px; font-weight:600; text-transform:uppercase; }} code {{ font:13px/1.45 ui-monospace,"SF Mono",Menlo,monospace; white-space:pre-wrap; }} .article-diff {{ background:var(--surface); border-radius:8px; font:12px/1.5 ui-monospace,"SF Mono",Menlo,monospace; margin:13px 0 0; max-height:430px; overflow:auto; padding:11px 13px; white-space:pre-wrap; }} .article-diff span {{ display:block; }} .diff-added {{ color:var(--added); }} .diff-removed {{ color:var(--removed); }} .diff-range,.diff-file {{ color:var(--secondary); }} .truncated {{ color:var(--secondary); font-size:12px; margin:7px 0 0; }} .change-card footer {{ margin-top:13px; }}
    .empty {{ color:var(--secondary); padding:36px 0; }}
    .site-footer {{ border-top:1px solid var(--line); color:var(--secondary); font-size:12px; padding:18px max(24px,calc((100vw - 980px)/2)); text-align:right; }} .site-footer a {{ color:inherit; text-decoration:none; }} .site-footer a:hover {{ text-decoration:underline; }}
    @media(prefers-color-scheme:dark) {{ :root {{ --page:#1d1d1f; --surface:#2c2c2e; --line:#48484a; --text:#f5f5f7; --secondary:#a1a1a6; --link:#2997ff; --added:#52b563; --modified:#ff9f45; --removed:#ff696f; }} }}
    @media(max-width:680px) {{ main {{ padding:32px 16px 56px; }} .controls,.comparison-heading {{ align-items:stretch; flex-direction:column; }} .control-group {{ display:grid; grid-template-columns:1fr 1fr; }} input {{ min-width:0; }} .site-header {{ align-items:flex-start; flex-direction:column; gap:8px; padding:10px 16px; }} .site-footer {{ padding:16px; text-align:left; }} }}
  </style>
</head>
<body>
  <header class="site-header"><a class="brand" href="{html.escape(docs_home)}">{html.escape(arguments.project_name)} <span>Documentation</span></a><div class="header-actions"><span class="header-meta">Built {html.escape(arguments.build_date)}</span>{star_link}</div></header>
  <main>
    <div class="controls"><div><p class="eyebrow">Documentation</p><strong>{html.escape(changes_title)}</strong></div><div class="control-group">
      <label class="{module_control_class}">Module<select id="module-scope">{module_options}</select></label>
      <label>Compare<select id="compare">{comparison_options}</select></label>
      <label class="{source_control_class}">Content<select id="source">{source_options}</select></label>
      <label>Show<select id="filter"><option value="all">All changes</option><option value="added">Added</option><option value="modified">Modified</option><option value="removed">Removed</option></select></label>
      <label>Search<input id="search" type="search" placeholder="Symbol, article, or declaration"></label>
    </div></div>
    <section><div class="comparison-heading"><div><p class="eyebrow">Version comparison</p><h1 id="title"></h1></div><p class="comparison-note" id="note"></p></div>
    <div class="summary-grid"><div class="summary added"><strong id="added"></strong><span>Added</span></div><div class="summary modified"><strong id="modified"></strong><span>Modified</span></div><div class="summary removed"><strong id="removed"></strong><span>Removed</span></div></div>
    <div class="result-bar"><span id="result-count"></span><div class="pager"><button id="previous">Previous</button><button id="next">Next</button></div></div><div id="changes" class="changes-list"></div><p id="empty" class="empty" hidden>No changes match this filter.</p></section>
  </main>
  {powered_by}
  <script>
    const DATA={data}; const BASE={json.dumps(base)}; const DEFAULT_VERSION={json.dumps(arguments.default_version)}; const SCOPE={json.dumps(selected_module['path'] if selected_module else None)}; const PAGE_SIZE={arguments.page_size}; let page=0;
    const moduleScope=document.getElementById('module-scope'), compare=document.getElementById('compare'), sourceFilter=document.getElementById('source'), filter=document.getElementById('filter'), search=document.getElementById('search'), list=document.getElementById('changes');
    const escapeHTML=(value)=>String(value??'').replace(/[&<>"']/g,(character)=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[character]));
    function activeComparison() {{ return DATA.comparisons.find(item=>item.id===compare.value)||DATA.comparisons[0]; }}
    function entity(change) {{ return change.current||change.previous; }}
    function scopedChanges(comparison,modulePath=SCOPE) {{ return comparison.changes.filter(change=>!modulePath||entity(change).modulePath===modulePath); }}
    function visibleChanges(comparison) {{ const query=search.value.trim().toLowerCase(); return scopedChanges(comparison).filter(change=>{{ const item=entity(change), searchable=(item.displayId+' '+item.title+' '+(item.declaration||'')+' '+(item.preview||'')+' '+(item.moduleName||'')).toLowerCase(); return (sourceFilter.value==='all'||change.source===sourceFilter.value)&&(filter.value==='all'||change.type===filter.value)&&(!query||searchable.includes(query)); }}); }}
    function articleDiff(change) {{
      if(change.type!=='modified') return `<div class="declaration"><span>${{escapeHTML(change.type==='removed'?'Previous':'Current')}} article</span><code>${{escapeHTML(entity(change).preview||'')}}</code></div>`;
      const lines=(change.diff||[]).map(line=>{{ let style=''; if(line.startsWith('+++')||line.startsWith('---')) style='diff-file'; else if(line.startsWith('+')) style='diff-added'; else if(line.startsWith('-')) style='diff-removed'; else if(line.startsWith('@@')) style='diff-range'; return `<span class="${{style}}">${{escapeHTML(line)||' '}}</span>`; }}).join('');
      return `<pre class="article-diff">${{lines}}</pre>${{change.diffTruncated?'<p class="truncated">Diff truncated after 160 lines.</p>':''}}`;
    }}
    function card(change,comparison) {{ const item=entity(change), linkVersion=change.type==='removed'?comparison.previousVersion:comparison.currentVersion, href=item.path?BASE+'/'+linkVersion+item.path:null, title=href?`<h2><a href="${{escapeHTML(href)}}">${{escapeHTML(item.title)}}</a></h2>`:`<h2>${{escapeHTML(item.title)}}</h2>`, documentationLink=href?`<footer><a href="${{escapeHTML(href)}}">View documentation →</a></footer>`:''; let details=''; if(change.source==='article') details=articleDiff(change); else if(change.type==='modified') details=`<div class="declaration"><span>Current · ${{escapeHTML(comparison.currentVersion)}}</span><code>+ ${{escapeHTML(change.current.declaration)}}</code></div><div class="declaration"><span>Previous · ${{escapeHTML(comparison.previousVersion)}}</span><code>− ${{escapeHTML(change.previous.declaration)}}</code></div>`; else details=`<div class="declaration"><span>${{escapeHTML(linkVersion)}}</span><code>${{escapeHTML(item.declaration)}}</code></div>`; const availability=item.availability?.length?`<p class="availability">${{escapeHTML(item.availability.join(' · '))}}</p>`:'', moduleBadge=!SCOPE&&DATA.modules.length>1&&item.moduleName?`<span class="change-module">${{escapeHTML(item.moduleName)}}</span>`:''; return `<article class="change-card ${{change.type}}"><header><span class="badge">${{change.type[0].toUpperCase()+change.type.slice(1)}}</span><span class="kind">${{escapeHTML(item.kind)}}</span>${{moduleBadge}}</header>${{title}}<p class="symbol-path">${{escapeHTML(item.displayId)}}</p>${{availability}}${{details}}${{documentationLink}}</article>`; }}
    function routeForModule(modulePath) {{ const suffix=modulePath?modulePath+'/':''; const parameters=new URLSearchParams(location.search); parameters.delete('page'); const query=parameters.toString(); return BASE+'/'+DEFAULT_VERSION+'/changes/'+suffix+(query?'?'+query:''); }}
    function countTypes(changes) {{ return Object.fromEntries(['added','modified','removed'].map(type=>[type,changes.filter(change=>change.type===type).length])); }}
    function restoreState() {{
      const parameters=new URLSearchParams(location.search), comparisonId=parameters.get('compare'), content=parameters.get('content'), show=parameters.get('show'), requestedPage=Number(parameters.get('page'));
      compare.value=DATA.comparisons.some(item=>item.id===comparisonId)?comparisonId:DATA.comparisons[0].id;
      sourceFilter.value=['all',...DATA.sources].includes(content)?content:'all';
      filter.value=['all','added','modified','removed'].includes(show)?show:'all';
      search.value=parameters.get('search')||'';
      page=Number.isInteger(requestedPage)&&requestedPage>0?requestedPage-1:0;
    }}
    function persistState() {{
      const parameters=new URLSearchParams(location.search), update=(name,value,isDefault)=>isDefault?parameters.delete(name):parameters.set(name,value);
      update('compare',compare.value,compare.value===DATA.comparisons[0].id);
      update('content',sourceFilter.value,sourceFilter.value==='all');
      update('show',filter.value,filter.value==='all');
      update('search',search.value,search.value==='');
      update('page',String(page+1),page===0);
      const query=parameters.toString();
      history.replaceState(null,'',location.pathname+(query?'?'+query:'')+location.hash);
    }}
    function render() {{ const comparison=activeComparison(), scopeChanges=scopedChanges(comparison), changes=visibleChanges(comparison), scopeCounts=countTypes(scopeChanges), pages=Math.max(1,Math.ceil(changes.length/PAGE_SIZE)); page=Math.min(page,pages-1); const start=page*PAGE_SIZE, shown=changes.slice(start,start+PAGE_SIZE), apiCount=scopeChanges.filter(change=>change.source==='api').length, articleCount=scopeChanges.filter(change=>change.source==='article').length, notes=[]; if(apiCount) notes.push(apiCount.toLocaleString()+' public API'); if(articleCount) notes.push(articleCount.toLocaleString()+' article'); document.getElementById('title').innerHTML=escapeHTML(comparison.previousVersion)+' <span>→</span> '+escapeHTML(comparison.currentVersion); document.getElementById('note').textContent=(notes.join(' · ')||'No')+' changes'; for(const type of ['added','modified','removed']) document.getElementById(type).textContent=scopeCounts[type].toLocaleString(); document.getElementById('result-count').textContent=changes.length?`Showing ${{start+1}}–${{start+shown.length}} of ${{changes.length.toLocaleString()}} changes`:'No matching changes'; document.getElementById('previous').disabled=page===0; document.getElementById('next').disabled=page>=pages-1; list.innerHTML=shown.map(change=>card(change,comparison)).join(''); document.getElementById('empty').hidden=changes.length!==0; persistState(); }}
    moduleScope.addEventListener('change',()=>window.location.assign(routeForModule(moduleScope.value))); compare.addEventListener('change',()=>{{page=0;render();}}); sourceFilter.addEventListener('change',()=>{{page=0;render();}}); filter.addEventListener('change',()=>{{page=0;render();}}); search.addEventListener('input',()=>{{page=0;render();}}); document.getElementById('previous').addEventListener('click',()=>{{page--;render();scrollTo(0,0);}}); document.getElementById('next').addEventListener('click',()=>{{page++;render();scrollTo(0,0);}});
    function restoreAndRender() {{ restoreState(); render(); }}
    window.addEventListener('pageshow',restoreAndRender);
    window.addEventListener('popstate',restoreAndRender);
    restoreAndRender();
  </script>
</body></html>\n"""


def main():
    arguments = parse_arguments()
    modules = parse_modules(arguments.module, arguments.module_path)
    graph_versions, graph_paths = parse_versioned_paths(arguments.symbol_graph)
    article_versions, article_paths = parse_versioned_paths(arguments.article_root)
    if not graph_versions and not article_versions:
        raise ValueError("at least one --symbol-graph or --article-root is required")
    versions = graph_versions or article_versions
    if graph_versions and article_versions and graph_versions != article_versions:
        raise ValueError("symbol graph and article versions must match")

    api_snapshots = {}
    for version in graph_versions:
        documentation_urls = load_docc_urls(
            arguments.output_root / version / "data" / "documentation"
        )
        api_snapshots[version] = merge_snapshots(
            graph_paths[version],
            arguments.module_path,
            documentation_urls,
            modules,
        )
    article_snapshots = {}
    for version in article_versions:
        if len(article_paths[version]) != 1:
            raise ValueError(f"{version} requires exactly one article root")
        article_snapshots[version] = article_snapshot(
            article_paths[version][0], modules
        )

    comparisons = []
    for index in range(len(versions) - 1):
        current_version = versions[index]
        previous_version = versions[index + 1]
        sources = []
        if graph_versions:
            sources.append(
                compare(
                    previous_version,
                    current_version,
                    api_snapshots[previous_version],
                    api_snapshots[current_version],
                )
            )
        if article_versions:
            sources.append(
                compare_articles(
                    previous_version,
                    current_version,
                    article_snapshots[previous_version],
                    article_snapshots[current_version],
                )
            )
        comparisons.append(merge_comparisons(sources))
    change_sources = [
        source
        for source, selected in (("api", graph_versions), ("article", article_versions))
        if selected
    ]
    manifest = {
        "schemaVersion": 3,
        "buildDate": arguments.build_date,
        "sources": change_sources,
        "modules": modules,
        "comparisons": comparisons,
    }
    arguments.output_root.mkdir(parents=True, exist_ok=True)
    serialized_manifest = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    (arguments.output_root / "changes.json").write_text(
        serialized_manifest, encoding="utf-8"
    )
    if graph_versions:
        legacy_manifest = {
            "schemaVersion": 1,
            "buildDate": arguments.build_date,
            "comparisons": [
                legacy_api_comparison(comparison) for comparison in comparisons
            ],
        }
        (arguments.output_root / "api-diffs.json").write_text(
            json.dumps(legacy_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    dashboard = render_dashboard(
        comparisons,
        arguments,
        change_sources,
        modules,
    )
    module_dashboards = {
        module["path"]: render_dashboard(
            comparisons,
            arguments,
            change_sources,
            modules,
            module["path"],
        )
        for module in modules
    }
    for version in versions:
        directory = arguments.output_root / version / "changes"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "index.html").write_text(dashboard, encoding="utf-8")
        if len(modules) > 1:
            for module_path, module_dashboard in module_dashboards.items():
                module_directory = directory / module_path
                module_directory.mkdir(parents=True, exist_ok=True)
                (module_directory / "index.html").write_text(
                    module_dashboard, encoding="utf-8"
                )
    for comparison in comparisons:
        counts = comparison["counts"]
        print(
            f"{comparison['previousVersion']} -> {comparison['currentVersion']}: "
            f"{counts['added']} added, {counts['modified']} modified, {counts['removed']} removed"
        )


if __name__ == "__main__":
    main()
