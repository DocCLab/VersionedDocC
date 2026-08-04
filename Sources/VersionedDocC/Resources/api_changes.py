#!/usr/bin/env python3

import argparse
import html
import json
from pathlib import Path


CHANGE_ORDER = {"added": 0, "modified": 1, "removed": 2}


def positive_integer(value):
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_arguments():
    parser = argparse.ArgumentParser(description="Generate a versioned public API changes site")
    parser.add_argument("--symbol-graph", action="append", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--hosting-base-path", required=True)
    parser.add_argument("--default-version", required=True)
    parser.add_argument("--build-date", required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--module-path", required=True)
    parser.add_argument("--page-size", type=positive_integer, default=10)
    return parser.parse_args()


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


def load_snapshot(path, module_path, documentation_urls=None):
    with path.open(encoding="utf-8") as source:
        graph = json.load(source)
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
            documentation_path = f"/documentation/{module_path}/{suffix}"
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
        }
    return snapshot


def merge_snapshots(paths, module_path, documentation_urls=None):
    """Merge platform-specific graphs, keeping the first graph as primary."""
    merged = {}
    identifiers_by_precise = {}
    for path in paths:
        snapshot = load_snapshot(path, module_path, documentation_urls)
        for identifier, item in snapshot.items():
            existing_identifier = identifiers_by_precise.get(item["precise"])
            if existing_identifier is not None:
                existing = merged[existing_identifier]
                existing["availability"] = list(
                    dict.fromkeys(existing["availability"] + item["availability"])
                )
                if existing.get("path") is None and item.get("path") is not None:
                    existing["path"] = item["path"]
                continue
            if identifier in merged:
                identifier = f"{item['displayId']}#{item['precise']}"
                item["id"] = identifier
            merged[identifier] = item
            identifiers_by_precise[item["precise"]] = identifier
    return merged


def compare(previous_version, current_version, previous, current):
    changes = []
    previous_ids = set(previous)
    current_ids = set(current)
    for identifier in current_ids - previous_ids:
        changes.append({"type": "added", "current": current[identifier]})
    for identifier in previous_ids - current_ids:
        changes.append({"type": "removed", "previous": previous[identifier]})
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


def render_dashboard(comparisons, arguments):
    base = arguments.hosting_base_path.rstrip("/")
    data = json.dumps({"comparisons": comparisons}, separators=(",", ":")).replace("</", "<\\/")
    comparison_options = "\n".join(
        f'<option value="{html.escape(item["id"])}">'
        f'{html.escape(item["previousVersion"])} → {html.escape(item["currentVersion"])}</option>'
        for item in comparisons
    )
    docs_home = f"{base}/{arguments.default_version}/documentation/{arguments.module_path}/"
    canonical = f"{base}/{arguments.default_version}/changes/"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="canonical" href="{html.escape(canonical)}">
  <title>API Changes | {html.escape(arguments.project_name)} Documentation</title>
  <style>
    :root {{ color-scheme: light dark; --page:#fff; --surface:#f5f5f7; --line:#d2d2d7; --text:#1d1d1f; --secondary:#6e6e73; --link:#06c; --added:#248a3d; --modified:#b65d00; --removed:#c72535; }}
    * {{ box-sizing:border-box; }} body {{ background:var(--page); color:var(--text); font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif; margin:0; }} a {{ color:var(--link); }}
    .site-header {{ align-items:center; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; min-height:52px; padding:8px max(24px,calc((100vw - 980px)/2)); }}
    .brand {{ color:inherit; font-weight:650; text-decoration:none; }} .brand span,.header-meta,.eyebrow,.comparison-note,.symbol-path,.availability {{ color:var(--secondary); }} .brand span {{ font-weight:400; margin-left:7px; }} .header-meta,.eyebrow,.symbol-path,.availability {{ font-size:12px; }}
    main {{ margin:0 auto; max-width:980px; padding:54px 24px 80px; }} .intro {{ background:var(--surface); border-radius:10px; color:var(--secondary); margin:0 0 32px; padding:13px 16px; }}
    .controls,.comparison-heading {{ align-items:end; display:flex; gap:16px; justify-content:space-between; }} .controls {{ border-bottom:1px solid var(--line); margin-bottom:36px; padding-bottom:24px; }} .control-group {{ display:flex; gap:12px; }} label {{ color:var(--secondary); display:grid; font-size:12px; gap:5px; }} select,input {{ background:var(--page); border:1px solid var(--line); border-radius:7px; color:var(--text); font:inherit; min-height:36px; padding:5px 10px; }} input {{ min-width:240px; }}
    .eyebrow {{ font-weight:600; letter-spacing:.06em; margin:0 0 4px; text-transform:uppercase; }} h1 {{ font-size:34px; letter-spacing:-.025em; margin:0; }} h1 span {{ color:var(--secondary); font-weight:400; }}
    .summary-grid {{ display:grid; gap:12px; grid-template-columns:repeat(3,1fr); margin:24px 0 28px; }} .summary {{ background:var(--surface); border-radius:12px; display:grid; padding:16px 18px; }} .summary strong {{ font-size:26px; line-height:1.1; }} .summary span {{ color:var(--secondary); font-size:13px; }} .added strong {{ color:var(--added); }} .modified strong {{ color:var(--modified); }} .removed strong {{ color:var(--removed); }}
    .result-bar,.pager {{ align-items:center; display:flex; justify-content:space-between; }} .result-bar {{ color:var(--secondary); margin:0 0 12px; }} .pager {{ gap:8px; }} button {{ background:var(--page); border:1px solid var(--line); border-radius:7px; color:var(--text); font:inherit; padding:7px 12px; }} button:disabled {{ opacity:.45; }}
    .changes-list {{ display:grid; gap:16px; }} .change-card {{ border:1px solid var(--line); border-left:4px solid; border-radius:10px; padding:18px 20px; }} .change-card.added {{ border-left-color:var(--added); }} .change-card.modified {{ border-left-color:var(--modified); }} .change-card.removed {{ border-left-color:var(--removed); }}
    .badge {{ border-radius:999px; color:#fff; font-size:11px; font-weight:650; padding:2px 8px; }} .added .badge {{ background:var(--added); }} .modified .badge {{ background:var(--modified); }} .removed .badge {{ background:var(--removed); }} .kind {{ color:var(--secondary); font-size:12px; margin-left:8px; }} h2 {{ font-size:19px; margin:10px 0 0; }} h2 a {{ color:inherit; text-decoration:none; }} .symbol-path,.availability {{ margin:2px 0; }} .symbol-path {{ font-family:ui-monospace,"SF Mono",monospace; }}
    .declaration {{ background:var(--surface); border-radius:8px; display:grid; gap:5px; margin-top:13px; padding:11px 13px; }} .declaration span {{ color:var(--secondary); font-size:11px; font-weight:600; text-transform:uppercase; }} code {{ font:13px/1.45 ui-monospace,"SF Mono",Menlo,monospace; white-space:pre-wrap; }} footer {{ margin-top:13px; }}
    .empty {{ color:var(--secondary); padding:36px 0; }}
    @media(prefers-color-scheme:dark) {{ :root {{ --page:#1d1d1f; --surface:#2c2c2e; --line:#48484a; --text:#f5f5f7; --secondary:#a1a1a6; --link:#2997ff; --added:#52b563; --modified:#ff9f45; --removed:#ff696f; }} }}
    @media(max-width:680px) {{ main {{ padding:32px 16px 56px; }} .controls,.comparison-heading {{ align-items:stretch; flex-direction:column; }} .control-group {{ display:grid; grid-template-columns:1fr 1fr; }} input {{ min-width:0; }} }}
  </style>
</head>
<body>
  <header class="site-header"><a class="brand" href="{html.escape(docs_home)}">{html.escape(arguments.project_name)} <span>Documentation</span></a><span class="header-meta">Built {html.escape(arguments.build_date)}</span></header>
  <main>
    <p class="intro">Generated from immutable public symbol-graph snapshots. Cached release documentation does not need to be rebuilt to render this comparison.</p>
    <div class="controls"><div><p class="eyebrow">Documentation</p><strong>API Changes</strong></div><div class="control-group">
      <label>Compare<select id="compare">{comparison_options}</select></label>
      <label>Show<select id="filter"><option value="all">All changes</option><option value="added">Added</option><option value="modified">Modified</option><option value="removed">Removed</option></select></label>
      <label>Search<input id="search" type="search" placeholder="Symbol or declaration"></label>
    </div></div>
    <section><div class="comparison-heading"><div><p class="eyebrow">Version comparison</p><h1 id="title"></h1></div><p class="comparison-note" id="note"></p></div>
    <div class="summary-grid"><div class="summary added"><strong id="added"></strong><span>Added</span></div><div class="summary modified"><strong id="modified"></strong><span>Modified</span></div><div class="summary removed"><strong id="removed"></strong><span>Removed</span></div></div>
    <div class="result-bar"><span id="result-count"></span><div class="pager"><button id="previous">Previous</button><button id="next">Next</button></div></div><div id="changes" class="changes-list"></div><p id="empty" class="empty" hidden>No changes match this filter.</p></section>
  </main>
  <script>
    const DATA={data}; const BASE={json.dumps(base)}; const PAGE_SIZE={arguments.page_size}; let page=0;
    const compare=document.getElementById('compare'), filter=document.getElementById('filter'), search=document.getElementById('search'), list=document.getElementById('changes');
    const escapeHTML=(value)=>String(value??'').replace(/[&<>"']/g,(character)=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[character]));
    function activeComparison() {{ return DATA.comparisons.find(item=>item.id===compare.value)||DATA.comparisons[0]; }}
    function symbol(change) {{ return change.current||change.previous; }}
    function visibleChanges(comparison) {{ const query=search.value.trim().toLowerCase(); return comparison.changes.filter(change=>{{ const item=symbol(change); return (filter.value==='all'||change.type===filter.value)&&(!query||(item.displayId+' '+item.declaration).toLowerCase().includes(query)); }}); }}
    function card(change,comparison) {{ const item=symbol(change), linkVersion=change.type==='removed'?comparison.previousVersion:comparison.currentVersion, href=item.path?BASE+'/'+linkVersion+item.path:null, title=href?`<h2><a href="${{escapeHTML(href)}}">${{escapeHTML(item.title)}}</a></h2>`:`<h2>${{escapeHTML(item.title)}}</h2>`, documentationLink=href?`<footer><a href="${{escapeHTML(href)}}">View documentation →</a></footer>`:''; let declarations=''; if(change.type==='modified') declarations=`<div class="declaration"><span>Current · ${{escapeHTML(comparison.currentVersion)}}</span><code>+ ${{escapeHTML(change.current.declaration)}}</code></div><div class="declaration"><span>Previous · ${{escapeHTML(comparison.previousVersion)}}</span><code>− ${{escapeHTML(change.previous.declaration)}}</code></div>`; else declarations=`<div class="declaration"><span>${{escapeHTML(linkVersion)}}</span><code>${{escapeHTML(item.declaration)}}</code></div>`; const availability=item.availability?.length?`<p class="availability">${{escapeHTML(item.availability.join(' · '))}}</p>`:''; return `<article class="change-card ${{change.type}}"><header><span class="badge">${{change.type[0].toUpperCase()+change.type.slice(1)}}</span><span class="kind">${{escapeHTML(item.kind)}}</span></header>${{title}}<p class="symbol-path">${{escapeHTML(item.displayId)}}</p>${{availability}}${{declarations}}${{documentationLink}}</article>`; }}
    function restoreState() {{
      const parameters=new URLSearchParams(location.search), comparisonId=parameters.get('compare'), show=parameters.get('show'), requestedPage=Number(parameters.get('page'));
      compare.value=DATA.comparisons.some(item=>item.id===comparisonId)?comparisonId:DATA.comparisons[0].id;
      filter.value=['all','added','modified','removed'].includes(show)?show:'all';
      search.value=parameters.get('search')||'';
      page=Number.isInteger(requestedPage)&&requestedPage>0?requestedPage-1:0;
    }}
    function persistState() {{
      const parameters=new URLSearchParams(location.search), update=(name,value,isDefault)=>isDefault?parameters.delete(name):parameters.set(name,value);
      update('compare',compare.value,compare.value===DATA.comparisons[0].id);
      update('show',filter.value,filter.value==='all');
      update('search',search.value,search.value==='');
      update('page',String(page+1),page===0);
      const query=parameters.toString();
      history.replaceState(null,'',location.pathname+(query?'?'+query:'')+location.hash);
    }}
    function render() {{ const comparison=activeComparison(), changes=visibleChanges(comparison), pages=Math.max(1,Math.ceil(changes.length/PAGE_SIZE)); page=Math.min(page,pages-1); const start=page*PAGE_SIZE, shown=changes.slice(start,start+PAGE_SIZE); document.getElementById('title').innerHTML=escapeHTML(comparison.previousVersion)+' <span>→</span> '+escapeHTML(comparison.currentVersion); document.getElementById('note').textContent=comparison.changes.length.toLocaleString()+' public API changes from real symbol graphs'; for(const type of ['added','modified','removed']) document.getElementById(type).textContent=comparison.counts[type].toLocaleString(); document.getElementById('result-count').textContent=changes.length?`Showing ${{start+1}}–${{start+shown.length}} of ${{changes.length.toLocaleString()}} changes`:'No matching changes'; document.getElementById('previous').disabled=page===0; document.getElementById('next').disabled=page>=pages-1; list.innerHTML=shown.map(change=>card(change,comparison)).join(''); document.getElementById('empty').hidden=changes.length!==0; persistState(); }}
    compare.addEventListener('change',()=>{{page=0;render();}}); filter.addEventListener('change',()=>{{page=0;render();}}); search.addEventListener('input',()=>{{page=0;render();}}); document.getElementById('previous').addEventListener('click',()=>{{page--;render();scrollTo(0,0);}}); document.getElementById('next').addEventListener('click',()=>{{page++;render();scrollTo(0,0);}});
    function restoreAndRender() {{ restoreState(); render(); }}
    window.addEventListener('pageshow',restoreAndRender);
    window.addEventListener('popstate',restoreAndRender);
    restoreAndRender();
  </script>
</body></html>\n"""


def main():
    arguments = parse_arguments()
    versions = []
    graph_paths = {}
    snapshots = {}
    for specification in arguments.symbol_graph:
        version, separator, raw_path = specification.partition("=")
        if not separator:
            raise ValueError(f"invalid symbol graph specification: {specification}")
        if version not in graph_paths:
            versions.append(version)
            graph_paths[version] = []
        graph_paths[version].append(Path(raw_path))
    for version in versions:
        documentation_urls = load_docc_urls(
            arguments.output_root / version / "data" / "documentation"
        )
        snapshots[version] = merge_snapshots(
            graph_paths[version], arguments.module_path, documentation_urls
        )
    comparisons = []
    for index in range(len(versions) - 1):
        current_version = versions[index]
        previous_version = versions[index + 1]
        comparisons.append(
            compare(
                previous_version,
                current_version,
                snapshots[previous_version],
                snapshots[current_version],
            )
        )
    manifest = {
        "schemaVersion": 1,
        "buildDate": arguments.build_date,
        "comparisons": comparisons,
    }
    arguments.output_root.mkdir(parents=True, exist_ok=True)
    (arguments.output_root / "api-diffs.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    dashboard = render_dashboard(comparisons, arguments)
    for version in versions:
        directory = arguments.output_root / version / "changes"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "index.html").write_text(dashboard, encoding="utf-8")
    for comparison in comparisons:
        counts = comparison["counts"]
        print(
            f"{comparison['previousVersion']} -> {comparison['currentVersion']}: "
            f"{counts['added']} added, {counts['modified']} modified, {counts['removed']} removed"
        )


if __name__ == "__main__":
    main()
