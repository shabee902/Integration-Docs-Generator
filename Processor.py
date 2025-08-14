#!/usr/bin/env python3
import argparse, sys, subprocess, os, textwrap
from pathlib import Path
from datetime import datetime
import xml.etree.ElementTree as ET

# Optional dependencies
try:
    import lxml.etree as LET
    HAVE_LXML = True
except Exception:
    HAVE_LXML = False

try:
    import yaml
    HAVE_YAML = True
except Exception:
    HAVE_YAML = False

try:
    from graphviz import Digraph
    HAVE_GV = True
except Exception:
    HAVE_GV = False

from jinja2 import Environment, FileSystemLoader, select_autoescape

HTML_TEMPLATE = "doc.html.j2"
STYLE_FILE = "style.css"


# ---------- CLI ----------
def parse_args():
    p = argparse.ArgumentParser(description="Process one XML into HTML/PDF")
    p.add_argument("--xml", required=True)
    p.add_argument("--outdir", required=True)
    p.add_argument("--mapping", required=True)
    p.add_argument("--templates", default="templates")
    p.add_argument("--wkhtmltopdf", default="wkhtmltopdf")
    return p.parse_args()


# ---------- XML helpers ----------
def load_xml(path: Path):
    data = path.read_bytes()
    if HAVE_LXML:
        parser = LET.XMLParser(recover=True, remove_blank_text=True, encoding="utf-8")
        return LET.fromstring(data, parser=parser)
    # fallback (limited): no XPath
    return ET.fromstring(data.decode("utf-8", errors="ignore"))

def ntext(node) -> str:
    if node is None:
        return ""
    if HAVE_LXML:
        return "".join(node.itertext()).strip()
    return (node.text or "").strip()

def eval_xpath(root, expr: str):
    """Best-effort XPath evaluation. Full XPath if lxml is present, else minimal path walk."""
    if HAVE_LXML:
        try:
            return root.xpath(expr)
        except Exception:
            return []
    # minimal path walk without predicates/attributes
    try:
        parts = [p for p in expr.strip("/").split("/") if p]
        cur = root
        for p in parts:
            if p.startswith("@"):
                return cur.attrib.get(p[1:], "")
            found = None
            for child in list(cur):
                tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                if tag == p:
                    found = child
                    break
            if not found:
                return ""
            cur = found
        return ntext(cur)
    except Exception:
        return ""

def xtext(root, xpath):
    if not HAVE_LXML:
        return ""
    try:
        vals = root.xpath(xpath)
        if not vals:
            return ""
        v = vals[0]
        if hasattr(v, "itertext"):
            return "".join(v.itertext()).strip()
        return str(v).strip()
    except Exception:
        return ""

def xcount(root, xpath):
    if not HAVE_LXML:
        return 0
    try:
        vals = root.xpath(xpath)
        return len(vals) if isinstance(vals, list) else 0
    except Exception:
        return 0


# ---------- Mapping ----------
def load_mapping(path: Path):
    if not HAVE_YAML:
        raise RuntimeError("PyYAML not installed. Run: pip install -r requirements.txt")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def clean_kv_fields(fields):
    return [f for f in fields if str(f.get("value", "")).strip() != ""]

def section_has_data(sec):
    if sec["type"] == "kv":
        return any(str(f.get("value", "")).strip() != "" for f in sec.get("fields", []))
    if sec["type"] == "table":
        return len(sec.get("rows", [])) > 0
    return True

def apply_mapping(root, mapping):
    doc = {"title": mapping.get("title") or "Document", "sections": []}
    try:
        if HAVE_LXML:
            name_attr = root.xpath("/datawizardprofile/@name")
            if name_attr:
                doc["title"] = (mapping.get("title") or "Document").replace("{{name}}", name_attr[0])
    except Exception:
        pass

    sections = mapping.get("sections", {})
    for sec_name, spec in sections.items():
        if isinstance(spec, dict) and "_list" in spec:
            list_expr = spec["_list"]
            columns = spec.get("columns", {})
            nodes = eval_xpath(root, list_expr)
            if not isinstance(nodes, list):
                nodes = []
            rows = []
            for node in nodes:
                row = {}
                for col_name, col_expr in columns.items():
                    if col_expr.startswith("@"):
                        val = getattr(node, "attrib", {}).get(col_expr[1:], "")
                    else:
                        if HAVE_LXML:
                            found = node.xpath(col_expr)
                            if isinstance(found, list) and found:
                                if hasattr(found[0], "itertext"):
                                    val = "".join(found[0].itertext()).strip()
                                else:
                                    val = str(found[0])
                            else:
                                val = ""
                        else:
                            val = ""
                    row[col_name] = val
                rows.append(row)
            sec = {"name": sec_name, "type": "table", "columns": list(columns.keys()), "rows": rows}
            if section_has_data(sec):
                doc["sections"].append(sec)
        elif isinstance(spec, dict):
            fields = []
            for field_name, expr in spec.items():
                if field_name == "_list":
                    continue
                val = eval_xpath(root, expr)
                if HAVE_LXML and isinstance(val, list):
                    if len(val) == 0:
                        val = ""
                    elif hasattr(val[0], "itertext"):
                        val = "".join(val[0].itertext()).strip()
                    else:
                        val = str(val[0])
                fields.append({"label": field_name, "value": val})
            fields = clean_kv_fields(fields)
            sec = {"name": sec_name, "type": "kv", "fields": fields}
            if section_has_data(sec):
                doc["sections"].append(sec)
    return doc


# ---------- Workflow (info, steps, diagram) ----------
def _wrap_label(s: str, width: int = 26) -> str:
    s = (s or "").replace("_", "_\u200b")  # allow breaks at underscores
    parts = textwrap.wrap(s, width=width)
    return "\n".join(parts) if parts else s

def extract_workflow_info(root):
    # Basic IO
    method = (xtext(root, "/datawizardprofile/responsesettings/responseunits/unit_http/http_method/text()")
              or xtext(root, "/datawizardprofile/agent/http_method/text()") or "POST").upper()
    url = xtext(root, "/datawizardprofile/agent/url/text()")
    mime = (xtext(root, "/datawizardprofile/agent/mime_type/text()")
            or xtext(root, "/datawizardprofile/agent/content_type/text()")
            or "application/xml")

    # Mapping size
    mapping_pairs = xcount(root, "/datawizardprofile/dataproperties/structuredefinition/mappinginformation/mappingentry")

    # Success / Error targets
    fwd = xtext(root, "/datawizardprofile/responsesettings/responseunits/unit_http/forward_profile/text()")
    err = xtext(root, "/datawizardprofile/responsesettings/responseunits/unit_http/error_profile/text()")

    # Other error signals that mean "there IS an error path"
    exc_prof   = xtext(root, "/datawizardprofile/dataproperties/exception_profile/text()")
    wf_err     = xtext(root, "/datawizardprofile/dataproperties/wfErrorName/text()")
    err_mail   = xtext(root, "/datawizardprofile/dataproperties/error_recipient/text()")

    has_error = bool(err or exc_prof or wf_err or err_mail)

    return {
        "method": method,
        "url": url,
        "mime": mime,
        "mapping_pairs": mapping_pairs,
        "forward_profile": fwd,
        "error_profile": err,
        "has_error": has_error,
    }

def build_steps(info):
    steps = []
    steps.append({"title":"Trigger",
                  "desc": f"Event-based HTTP {info['method']} to `{info['url']}` with `{info['mime']}` payload."
                          if info.get('url') else
                          f"Event-based HTTP {info['method']} with `{info['mime']}` payload."})
    steps.append({"title":"Parse XML", "desc":"Read and validate structure (record tag, encoding, namespaces)."})
    steps.append({"title":"Validate Data", "desc":"Check required fields and formats; reject incomplete or malformed payloads."})
    steps.append({"title":"Map Fields", "desc": f"Apply mapping rules from source → target ({info.get('mapping_pairs',0)} pairs discovered)."})
    if info.get("forward_profile"):
        steps.append({"title":"Forward / Handoff", "desc": f"On success, hand off to `{info['forward_profile']}`."})
    if info.get("has_error"):
        steps.append({"title":"Error Handling",
                      "desc": f"On error, route to `{info['error_profile']}` for diagnostics and notifications."
                              if info.get("error_profile") else
                              "On error, execute the configured exception workflow or notifications."})
    return steps

def render_workflow_svg(info, outdir: Path, stem: str) -> str | None:
    """Render the workflow as a bounded SVG via Graphviz so it always fits the page."""
    if not HAVE_GV:
        return None
    try:
        import os
        from graphviz import Digraph

        # Ensure Python sees Graphviz even if PATH wasn't inherited
        gv_bin = r"C:\Program Files\Graphviz\bin"
        os.environ["PATH"] = gv_bin + os.pathsep + os.environ.get("PATH", "")

        g = Digraph("flow", format="svg")

        # Layout tuned to fit A4 content width with your margins
        g.attr(rankdir="LR")
        g.attr(
            "graph",
            dpi="110",
            ranksep="0.50",       # tighter gaps between ranks
            nodesep="0.30",       # tighter gaps between nodes
            margin="0.05",
            size="5.8,2.2!"       # hard cap width x height (inches); "!" forces scale-to-fit
        )
        g.attr(
            "node",
            shape="box",
            style="rounded",
            fontsize="10",
            fontname="Arial",
            width="1.7",          # smaller minimum node size
            height="0.55"
        )
        g.attr("edge", arrowsize="0.6")

        # Labels (wrapped so nodes don't stretch)
        n0_label = (
            f"HTTP {info['method']}\n{_wrap_label(info.get('url', ''), 22)}"
            if info.get("url") else f"HTTP {info['method']}"
        )
        n2_label = (
            f"Map Fields\n({info.get('mapping_pairs', 0)} pairs)"
            if info.get("mapping_pairs") else "Map Fields"
        )
        n3_label = (
            f"Forward → {_wrap_label(info.get('forward_profile', ''), 24)}"
            if info.get("forward_profile") else "Forward"
        )
        nE_label = (
            f"Error → {_wrap_label(info.get('error_profile', ''), 24)}"
            if info.get("error_profile") else "Error"
        )

        # Nodes
        g.node("n0", n0_label)
        g.node("n1", "Parse XML")
        g.node("n2", n2_label)
        g.node("n3", n3_label)

        if info.get("has_error"):
            nE_label = (
                f"Error → {_wrap_label(info.get('error_profile',''), 24)}"
                if info.get("error_profile") else "Error"
            )
            g.node("nE", nE_label, color="#B91C1C", fontcolor="#B91C1C")

        # Edges
        g.edge("n0", "n1", label=info.get("mime") or "payload")
        g.edge("n1", "n2")
        g.edge("n2", "n3")
        if info.get("has_error"):
            g.edge("n2", "nE", style="dashed", color="#B91C1C", label="on error")

        # Write to output/images
        img_dir = outdir / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{stem}_flow"
        g.render(filename=filename, directory=str(img_dir), cleanup=True)

        svg_path = img_dir / f"{filename}.svg"
        return str(svg_path) if svg_path.exists() else None

    except Exception:
        return None

# ---------- HTML/PDF ----------
def render_html(env, data):
    template = env.get_template(HTML_TEMPLATE)
    return template.render(data=data, css_path=STYLE_FILE, generated=datetime.now().strftime("%Y-%m-%d %H:%M"))

def write_bytes(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)

def try_pdf_from_html(html_path: Path, pdf_path: Path, wkhtmltopdf: str) -> bool:
    try:
        header = (Path("templates") / "header.html").resolve().as_uri()
        footer = (Path("templates") / "footer.html").resolve().as_uri()
        args = [
            wkhtmltopdf,
            "--enable-local-file-access",
            "--print-media-type",
            "--margin-top", "20mm",
            "--margin-bottom", "16mm",
            "--header-html", header,
            "--footer-html", footer,
            str(html_path.resolve()),
            str(pdf_path.resolve()),
        ]
        res = subprocess.run(args, capture_output=True, text=True)
        return res.returncode == 0 and pdf_path.exists() and pdf_path.stat().st_size > 0
    except Exception:
        return False

def try_weasyprint(html_path: Path, pdf_path: Path) -> bool:
    try:
        from weasyprint import HTML
        HTML(filename=str(html_path)).write_pdf(str(pdf_path))
        return pdf_path.exists() and pdf_path.stat().st_size > 0
    except Exception:
        return False

def try_reportlab_text_only(html_path: Path, pdf_path: Path) -> bool:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.lib.units import mm
        txt = html_path.read_text(encoding="utf-8", errors="ignore")
        c = canvas.Canvas(str(pdf_path), pagesize=A4)
        width, height = A4
        x = 20 * mm
        y = height - 20 * mm
        for line in txt.splitlines():
            if y < 20 * mm:
                c.showPage(); y = height - 20 * mm
            c.drawString(x, y, line[:120]); y -= 6 * mm
        c.save(); return True
    except Exception:
        return False

def xpath_bool(root, expr: str) -> bool:
    if not HAVE_LXML:
        return False
    try:
        vals = root.xpath(expr)
        if isinstance(vals, list):
            return len(vals) > 0 and (str(vals[0]).strip() != "" or hasattr(vals[0], "tag"))
        return bool(vals)
    except Exception:
        return False

def build_profile_purpose_section(root):
    """
    Returns a KV section dict like:
    {"name": "Profile Purpose", "type": "kv", "fields": [{"label":"Overview","value":"..."}]}
    """
    # Signals
    cc = xtext(root, "//custom_class/text()")
    has_cc = xpath_bool(root, "//custom_class[text()]")  # any custom class
    method = (xtext(root, "/datawizardprofile/responsesettings/responseunits/unit_http/http_method/text()")
              or xtext(root, "/datawizardprofile/agent/http_method/text()") or "").upper()
    url = xtext(root, "/datawizardprofile/agent/url/text()")
    mime = (xtext(root, "/datawizardprofile/agent/mime_type/text()")
            or xtext(root, "/datawizardprofile/agent/content_type/text()") or "")

    # Case 1: Phase 1 custom class (e.g., QuickReport) that extracts profile info
    # (This is the case you mentioned for 01_Mandanten_Kennung_aus_Lobster_Profilen)
    if has_cc:
        overview = (
            f"This profile extracts profile information using the custom class '{cc}' in Phase 1. "
            f"It gathers identifiers/attributes from existing Lobster profiles and produces a consolidated output for follow-up processing."
        )

    # Case 2: HTTP/API import profile (typical mapping flow)
    elif method or url:
        meth = method or "POST"
        overview = (
            f"This profile receives {meth} requests{(' at ' + url) if url else ''}"
            f"{(' with ' + mime) if mime else ''} and maps the incoming data to the internal format for downstream processing."
        )

    # Fallback
    else:
        ptype = xtext(root, "/datawizardprofile/type/text()")
        overview = (
            f"This profile is a scheduled or automated process ({ptype or 'unspecified'}). "
            f"It transforms source data into the standardized internal schema for later steps."
        )

    return {
        "name": "Profile Purpose",
        "type": "kv",
        "fields": [{"label": "Overview", "value": overview}]
    }


# ---------- main ----------
def main():
    args = parse_args()
    xml_path = Path(args.xml)
    outdir = Path(args.outdir)
    mapping_path = Path(args.mapping)
    templates_dir = Path(args.templates)
    outdir.mkdir(exist_ok=True, parents=True)

    # 1) Parse input + mapping
    root = load_xml(xml_path)
    mapping = load_mapping(mapping_path)
    data = apply_mapping(root, mapping)

    # 2) Build dynamic "Profile Purpose" and replace any generic section
    purpose = build_profile_purpose_section(root)
    data["sections"] = [
        s for s in data["sections"]
        if s.get("name") not in ("Business Context", "Profile Purpose")
    ]
    data["sections"].insert(0, purpose)

    # 3) Workflow (steps + diagram)
    info = extract_workflow_info(root)
    data["steps"] = build_steps(info)
    diagram_path = render_workflow_svg(info, outdir, xml_path.stem)
    if diagram_path:
        rel = Path(diagram_path).relative_to(outdir)
        data["workflow_image"] = str(rel).replace("\\", "/")
    else:
        data["workflow_image"] = ""

    # 4) Metadata for the template
    data["source_file"] = xml_path.name
    if not data.get("title"):
        data["title"] = xml_path.stem

    # 5) Render HTML and PDF
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(['html', 'xml'])
    )
    html = render_html(env, data)
    html_path = outdir / f"{xml_path.stem}.html"
    pdf_path = outdir / f"{xml_path.stem}.pdf"
    write_bytes(html_path, html.encode("utf-8"))

    if try_pdf_from_html(html_path, pdf_path, args.wkhtmltopdf) or \
       try_weasyprint(html_path, pdf_path) or \
       try_reportlab_text_only(html_path, pdf_path):
        print(f"[OK] Generated: {pdf_path.name}")
        sys.exit(0)

    print("[WARN] HTML created; PDF not generated.")
    sys.exit(1)


if __name__ == "__main__":
    main()
