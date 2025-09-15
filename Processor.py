#!/usr/bin/env python3
import argparse, sys, subprocess, os, textwrap, base64
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
    return ET.fromstring(data.decode("utf-8", errors="ignore"))

def ntext(node) -> str:
    if node is None:
        return ""
    if HAVE_LXML:
        return "".join(node.itertext()).strip()
    return (node.text or "").strip()

def eval_xpath(root, expr: str):
    if HAVE_LXML:
        try:
            return root.xpath(expr)
        except Exception:
            return []
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


# ---------- Helpers for formulas ----------
def _t(node):
    return "".join(node.itertext()).strip() if hasattr(node, "itertext") else (str(node) if node else "")

def _first_text(node_list):
    if not node_list:
        return ""
    v = node_list[0]
    return _t(v)

def _b64_or_text(s: str) -> str:
    if not s or not isinstance(s, str):
        return s or ""
    t = s.strip()
    if len(t) % 4 == 0 and all(c.isalnum() or c in "+/=\n\r" for c in t):
        try:
            dec = base64.b64decode(t).decode("utf-8", errors="ignore")
            return dec if dec.strip() else s
        except Exception:
            return s
    return s

def explain_rule(rule_name: str, params: list[str]) -> str:
    rn = (rule_name or "").strip().lower()
    # Shorten direct copies to "1:1" to save space
    if rn in ("copy", "identity", "") and not params:
        return "1:1"
    if rn in ("default", "const", "constant"):
        return f"Uses a fixed value: {params[0]!r}" if params else "Uses a fixed value."
    if rn in ("concat", "concatenate"):
        return f"Concatenation of parts: {', '.join(params)}."
    if rn in ("substr", "substring"):
        if len(params) >= 3:
            return f"Substring of source starting at {params[1]} with length {params[2]}."
        return "Substring of source."
    if rn in ("replace", "regexreplace", "regex_replace"):
        if len(params) >= 3:
            return f"Replace {params[1]!r} with {params[2]!r} in source."
        return "Text replacement on source."
    if rn in ("upper", "uppercase"):
        return "Uppercased from source."
    if rn in ("lower", "lowercase"):
        return "Lowercased from source."
    if rn in ("trim", "strip"):
        return "Trimmed whitespace from source."
    if rn in ("sum", "add"):
        return f"Sum of {', '.join(params)}."
    if rn in ("multiply", "mul"):
        return f"Product of {', '.join(params)}."
    if rn in ("round", "ceil", "floor"):
        return f"{rule_name.capitalize()} applied to numeric source."
    if rn in ("dateformat", "date_format", "formatdate"):
        if len(params) >= 3:
            return f"Date reformatted from {params[1]} to {params[2]}."
        return "Date reformatted."
    if rn in ("if", "case", "when"):
        return "Conditional mapping based on business rules."
    if rn in ("lookup", "map", "dictionary"):
        return "Value mapped via lookup table."
    if rn in ("boolean", "toboolean", "bool"):
        return "Converted to boolean from source."
    return f"Calculated using function '{rule_name}'" + (f" (params: {', '.join(params)})" if params else "")


# ---------- Description index (outputtree preferred) ----------
def build_description_index(root):
    """
    Returns dict { field_name: description_text }.
    Scans <outputtree>//node and falls back to <inputtree>//node for a `description` attribute.
    """
    def collect(tree_xpath):
        idx = {}
        if not HAVE_LXML:
            return idx
        try:
            nodes = root.xpath(f"{tree_xpath}//node")
        except Exception:
            nodes = []
        for n in nodes or []:
            name = (n.get("name") or "").strip()
            if not name:
                continue
            desc = (n.get("description") or "").strip()
            if desc:
                idx[name] = desc
        return idx

    out_idx = collect("//outputtree")
    in_idx  = collect("//inputtree")
    merged = dict(in_idx)
    merged.update(out_idx)  # outputtree wins
    return merged


# ---------- Formula index (outputtree preferred) ----------
def build_formula_index(root):
    """
    Returns dict { target_field_name: human_explanation }
    Reads <outputtree>//field[@name]/filter[...] first, then falls back to <inputtree>//field.
    """
    def collect_from_tree(tree_xpath):
        idx = {}
        if not HAVE_LXML:
            return idx
        try:
            fields = root.xpath(f"{tree_xpath}//field")
        except Exception:
            fields = []

        for f in fields or []:
            fname = f.get("name") or ""
            if not fname:
                continue

            # Gather all <filter> blocks for this field
            try:
                filters = f.xpath("./filter")
            except Exception:
                filters = []

            parts = []
            for flt in filters:
                desc = flt.get("description") or ""
                # ordered args: <a>, <b>, <c>, ...
                childs = [c for c in flt if hasattr(c, "tag")]
                childs.sort(key=lambda n: n.tag if hasattr(n, "tag") else "")
                args = []
                for ch in childs:
                    ch_type = (ch.get("type") or "").strip().lower()
                    ch_fc   = (ch.get("fieldconstant") or "").strip()
                    try:
                        vnodes = ch.xpath("./value/text()")
                        b64val = vnodes[0] if isinstance(vnodes, list) and vnodes else ""
                    except Exception:
                        b64val = ""
                    val = _b64_or_text(b64val)

                    if ch_type in ("linked field", "linked_field", "field", "source"):
                        if ch_fc:
                            args.append(f"field {ch_fc}")
                        elif val:
                            args.append(f"field {val}")
                        else:
                            args.append("linked field")
                    elif ch_type in ("constant", "const", "fixed"):
                        if val:
                            args.append(f"constant {repr(val)}")
                        elif ch_fc:
                            args.append(f"constant {repr(ch_fc)}")
                        else:
                            args.append("constant")
                    else:
                        if val:
                            args.append(f"{ch_type or 'value'} {repr(val)}")
                        elif ch_fc:
                            args.append(f"{ch_type or 'param'} {repr(ch_fc)}")
                        else:
                            args.append(ch_type or "param")

                if desc:
                    parts.append(f"{desc}: " + ", ".join(args) + "." if args else desc + ".")
                elif args:
                    parts.append("Calculated from: " + ", ".join(args) + ".")
                else:
                    parts.append("Calculated using configured filter.")

            if parts:
                idx[fname] = " ".join(parts)
            else:
                # Constants on field itself (rare, but keep)
                fix = (f.get("fix_value") or "").strip()
                if fix:
                    idx[fname] = f"Uses a fixed value: {repr(_b64_or_text(fix))}."
        return idx

    out_idx = collect_from_tree("//outputtree")
    in_idx  = collect_from_tree("//inputtree")

    merged = dict(in_idx)
    merged.update(out_idx)
    return merged


# ---------- Mapping extractor ----------
def extract_mapping_with_formulas(root):
    rows = []
    if not HAVE_LXML:
        return rows

    entry_paths = [
        "//structuredefinition/mappinginformation/mappingentry",
        "//mappinginformation/mappingentry",
        "//mappingentry"
    ]
    entries = []
    for ep in entry_paths:
        try:
            entries = root.xpath(ep)
            if entries:
                break
        except Exception:
            pass
    if not entries:
        return rows

    # Build indices
    formula_idx = build_formula_index(root)
    desc_idx = build_description_index(root)

    def normalize(name: str) -> str:
        if not name:
            return ""
        n = name
        if n.endswith("_val") or n.endswith("_attr"):
            n = n.rsplit("_", 1)[0]
        return n

    def lookup_desc(tgt: str) -> str:
        if not tgt:
            return ""
        if tgt in desc_idx:
            return desc_idx[tgt]
        t_norm = normalize(tgt).split("#", 1)[0]
        for k, v in desc_idx.items():
            k_norm = normalize(k).split("#", 1)[0]
            if t_norm == k_norm and v:
                return v
        return ""

    for e in entries:
        source = e.get("source") or e.get("from") or e.get("input") or e.get("src") or ""
        target = e.get("destination") or e.get("target") or e.get("to") or e.get("output") or e.get("tgt") or ""
        if not source:
            for sp in ["./source", "./input", "./from", "./src"]:
                try:
                    v = e.xpath(sp)
                    if v:
                        source = _first_text(v) if isinstance(v, list) else str(v)
                        break
                except: pass
        if not target:
            for tp in ["./target", "./output", "./to", "./tgt"]:
                try:
                    v = e.xpath(tp)
                    if v:
                        target = _first_text(v) if isinstance(v, list) else str(v)
                        break
                except: pass

        # rule / params (optional)
        rule_name, params = "", []
        for rp in ["./rule/@name", "./function/@name", "./mappingrule/@name", "./rule/name/text()"]:
            try:
                v = e.xpath(rp)
                if v:
                    rule_name = str(v[0]) if isinstance(v, list) else str(v)
                    break
            except: pass
        if not rule_name:
            for rt in ["./rule/text()", "./function/text()", "./mappingrule/text()"]:
                try:
                    v = e.xpath(rt)
                    if v:
                        rule_name = _t(v[0]) if isinstance(v, list) else str(v)
                        break
                except: pass
        for pp in ["./rule/param/text()", "./params/param/text()", "./function/param/text()", "./mappingrule/param/text()"]:
            try:
                vals = e.xpath(pp)
                if vals:
                    for val in vals:
                        txt = _t(val)
                        if txt: params.append(txt)
            except: pass
        if not rule_name and source and target:
            rule_name = "copy"

        # Start with function-based explanation
        formula = explain_rule(rule_name, params)

        # Prefer formula from output/input tree by matching the TARGET field name
        if target:
            if target in formula_idx:
                formula = formula_idx[target]
            else:
                t_norm = normalize(target).split("#", 1)[0]
                for key, expl in formula_idx.items():
                    k_norm = normalize(key).split("#", 1)[0]
                    if t_norm == k_norm:
                        formula = expl
                        break

        desc = lookup_desc(target)

        rows.append({
            "Source Structure": source or "(n/a)",
            "Target Structure": target or "(n/a)",
            "Description": desc,                         # NEW COLUMN
            "How it’s calculated": formula
        })

    return rows

def apply_mapping(root, mapping):
    doc = {"title": mapping.get("title") or "Document", "sections": []}
    try:
        if HAVE_LXML:
            name_attr = root.xpath("/datawizardprofile/@name")
            if name_attr:
                doc["title"] = (mapping.get("title") or "Document").replace("{{name}}", name_attr[0])
    except Exception:
        pass

    # template reads this; set the value in main()
    doc["source_file"] = doc.get("source_file", "")

    sections = mapping.get("sections", {})
    for sec_name, spec in sections.items():
        if isinstance(spec, dict) and sec_name.strip().lower() == "mapping" and spec.get("_auto") == "with_formulas":
            rows = extract_mapping_with_formulas(root)
            sec = {"name": "Mapping", "type": "table",
                   "columns": ["Source Structure", "Target Structure", "Description", "How it’s calculated"],
                   "rows": rows}
            if section_has_data(sec):
                doc["sections"].append(sec)
            continue

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


# ---------- Workflow + Profile Purpose ----------
def _wrap_label(s: str, width: int = 26) -> str:
    s = (s or "").replace("_", "_\u200b")
    parts = textwrap.wrap(s, width=width)
    return "\n".join(parts) if parts else s

def extract_workflow_info(root):
    method = (xtext(root, "/datawizardprofile/responsesettings/responseunits/unit_http/http_method/text()")
              or xtext(root, "/datawizardprofile/agent/http_method/text()") or "POST").upper()
    url = xtext(root, "/datawizardprofile/agent/url/text()")
    mime = (xtext(root, "/datawizardprofile/agent/mime_type/text()")
            or xtext(root, "/datawizardprofile/agent/content_type/text()")
            or "application/xml")
    mapping_pairs = xcount(root, "/datawizardprofile/dataproperties/structuredefinition/mappinginformation/mappingentry")
    fwd = xtext(root, "/datawizardprofile/responsesettings/responseunits/unit_http/forward_profile/text()")
    err = xtext(root, "/datawizardprofile/responsesettings/responseunits/unit_http/error_profile/text()")
    exc_prof   = xtext(root, "/datawizardprofile/dataproperties/exception_profile/text()")
    wf_err     = xtext(root, "/datawizardprofile/dataproperties/wfErrorName/text()")
    err_mail   = xtext(root, "/datawizardprofile/dataproperties/error_recipient/text()")
    has_error = bool(err or exc_prof or wf_err or err_mail)
    return {
        "method": method, "url": url, "mime": mime,
        "mapping_pairs": mapping_pairs,
        "forward_profile": fwd, "error_profile": err,
        "has_error": has_error,
    }

def build_steps(info):
    steps = []
    steps.append({"title":"Trigger",
                  "desc": f"Event-based HTTP {info['method']} to `{info['url']}` with `{info['mime']}` payload."
                          if info.get('url') else
                          f"Event-based HTTP {info['method']} with `{info['mime']}` payload."})
    steps.append({"title":"Parse XML", "desc":"Read and validate structure."})
    steps.append({"title":"Validate Data", "desc":"Check required fields and formats."})
    steps.append({"title":"Map Fields", "desc": f"Apply mapping rules ({info.get('mapping_pairs',0)} pairs)."})
    if info.get("forward_profile"):
        steps.append({"title":"Forward / Handoff", "desc": f"On success, hand off to `{info['forward_profile']}`."})
    if info.get("has_error"):
        steps.append({"title":"Error Handling",
                      "desc": f"On error, route to `{info['error_profile']}`."
                              if info.get("error_profile") else
                              "On error, execute the configured exception workflow."})
    return steps


# ---------- NEW: Input Data (3 bullet points) ----------
def detect_input_type_and_details(root, info):
    """
    Returns (type, details) where type in {"api","sftp","email","sharepoint","files"}.
    'info' is the dict from extract_workflow_info().
    """
    # API if method/url/mime present
    if info.get("method") or info.get("url"):
        return "api", {
            "endpoint": (info.get("url") or "").strip(),
            "method": (info.get("method") or "POST").upper(),
            "mime": (info.get("mime") or "application/xml"),
        }

    # Heuristics for SFTP / folder
    host = xtext(root, "//sftp_host/text()") or xtext(root, "//ftp_host/text()") or xtext(root, "//host/text()")
    path = xtext(root, "//path/text()") or xtext(root, "//directory/text()") or xtext(root, "//folder/text()")
    if host or path:
        return "sftp", {"host": host, "path": path}

    # Email
    mailbox = (xtext(root, "//email_address/text()")
               or xtext(root, "//mailbox/text()")
               or xtext(root, "//responsesettings/responseunits/unit_email/address/text()"))
    if mailbox:
        return "email", {"mailbox": mailbox}

    # SharePoint / Manual upload
    sp_site = xtext(root, "//sharepoint//site/text()") or xtext(root, "//office365//site/text()")
    sp_folder = xtext(root, "//sharepoint//folder/text()") or xtext(root, "//office365//folder/text()")
    if sp_site or sp_folder:
        return "sharepoint", {"site": sp_site, "folder": sp_folder}

    # Default generic "files" drop
    return "files", {"path": path or ""}

def build_input_data_fields(root, info):
    itype, det = detect_input_type_and_details(root, info)

    if itype == "api":
        endpoint = det.get("endpoint") or "the configured endpoint"
        method = det.get("method") or "POST"
        mime = det.get("mime") or "application/xml"
        return [
            {"label": "1. Where the data comes from", "value": f"Requests are sent to the endpoint `{endpoint}`."},
            {"label": "2. How the data is sent",      "value": f"The system expects an HTTP {method} request with the format {mime}."},
            {"label": "3. What must be included",     "value": "Each request contains a single order message that follows the agreed XML structure and includes the required fields (for example: order number, customer, items, quantities, and dates)."},
        ]

    if itype == "sftp":
        host = det.get("host") or "the SFTP server"
        path = det.get("path") or "the shared folder"
        return [
            {"label": "1. Where the data comes from", "value": f"Files are dropped to `{host}` at `{path}`."},
            {"label": "2. How the data is sent",      "value": "XML files encoded in UTF-8 with a consistent naming pattern (for example: `Orders_YYYYMMDD_HHMMSS_*.xml`)."},
            {"label": "3. What must be included",     "value": "Each file contains one order message that follows the agreed structure and includes the required fields."},
        ]

    if itype == "email":
        mailbox = det.get("mailbox") or "the designated mailbox"
        return [
            {"label": "1. Where the data comes from", "value": f"Messages are sent to {mailbox}."},
            {"label": "2. How the data is sent",      "value": "Email with an XML attachment (UTF-8)."},
            {"label": "3. What must be included",     "value": "The attachment contains one order message that follows the agreed structure and includes the required fields."},
        ]

    if itype == "sharepoint":
        site = det.get("site") or "the SharePoint site"
        folder = det.get("folder") or "the target folder"
        return [
            {"label": "1. Where the data comes from", "value": f"Files are uploaded to {site} in the folder {folder}."},
            {"label": "2. How the data is sent",      "value": "XML files uploaded through SharePoint (UTF-8)."},
            {"label": "3. What must be included",     "value": "Each file contains one order message with the required fields as defined in the mapping."},
        ]

    # generic files
    return [
        {"label": "1. Where the data comes from", "value": "Files are placed in the configured input folder."},
        {"label": "2. How the data is sent",      "value": "XML files encoded in UTF-8 with a stable naming pattern (for example: `Orders_YYYYMMDD_*.xml`)."},
        {"label": "3. What must be included",     "value": "Each file includes a single order message with the required fields."},
    ]

def build_input_data_section(root, info):
    fields = build_input_data_fields(root, info)
    return {"name": "Input Data", "type": "kv", "fields": fields}


def render_workflow_svg(info, outdir: Path, stem: str) -> str | None:
    if not HAVE_GV:
        return None
    try:
        from graphviz import Digraph

        CONTENT_W_IN = 7.1
        MAX_H_IN = 2.4

        g = Digraph("flow", format="svg")
        g.attr(rankdir="LR")
        g.attr(
            "graph",
            dpi="110",
            ranksep="0.45",
            nodesep="0.30",
            margin="0.04",
            pad="0.04",
            size=f"{CONTENT_W_IN},{MAX_H_IN}",
            ratio="compress"
        )
        g.attr("node", shape="box", style="rounded", fontsize="10", fontname="Arial")
        g.attr("edge", arrowsize="0.6")

        n0_label = f"HTTP {info['method']}\n{_wrap_label(info.get('url',''), 22)}" if info.get("url") else f"HTTP {info['method']}"
        n2_label = f"Map Fields\n({info.get('mapping_pairs', 0)} pairs)" if info.get("mapping_pairs") else "Map Fields"
        n3_label = f"Forward → {_wrap_label(info.get('forward_profile',''), 24)}" if info.get("forward_profile") else "Forward"
        nE_label = f"Error → {_wrap_label(info.get('error_profile',''), 24)}" if info.get("error_profile") else "Error"

        g.node("n0", n0_label)
        g.node("n1", "Parse XML")
        g.node("n2", n2_label)
        g.node("n3", n3_label)
        if info.get("has_error"):
            g.node("nE", nE_label, color="#B91C1C", fontcolor="#B91C1C")

        g.edge("n0", "n1", label=info.get("mime") or "payload")
        g.edge("n1", "n2")
        g.edge("n2", "n3")
        if info.get("has_error"):
            g.edge("n2", "nE", style="dashed", color="#B91C1C", label="on error")

        img_dir = outdir / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{stem}_flow"
        g.render(filename=filename, directory=str(img_dir), cleanup=True)

        svg_path = img_dir / f"{filename}.svg"
        return str(svg_path) if svg_path.exists() else None
    except Exception:
        return None

# --- HTML render (CSS href passed explicitly) ---
def render_html(env, data, css_href: str):
    template = env.get_template(HTML_TEMPLATE)
    return template.render(
        data=data,
        css_path=css_href,
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )

def write_bytes(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)

def try_pdf_from_html(html_path: Path, pdf_path: Path, wkhtmltopdf: str) -> bool:
    try:
        header = (Path("templates") / "header.html").resolve().as_uri()
        footer = (Path("templates") / "footer.html").resolve().as_uri()
        args = [wkhtmltopdf, "--enable-local-file-access", "--print-media-type",
                "--margin-top","20mm","--margin-bottom","16mm",
                "--header-html", header,"--footer-html", footer,
                str(html_path.resolve()), str(pdf_path.resolve())]
        res = subprocess.run(args, capture_output=True, text=True)
        return res.returncode==0 and pdf_path.exists() and pdf_path.stat().st_size>0
    except Exception:
        return False

def try_weasyprint(html_path: Path, pdf_path: Path) -> bool:
    try:
        from weasyprint import HTML
        HTML(filename=str(html_path)).write_pdf(str(pdf_path))
        return pdf_path.exists() and pdf_path.stat().st_size>0
    except Exception:
        return False

def try_reportlab_text_only(html_path: Path, pdf_path: Path) -> bool:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.lib.units import mm
        txt = html_path.read_text(encoding="utf-8", errors="ignore")
        c = canvas.Canvas(str(pdf_path), pagesize=A4)
        width,height = A4; x=20*mm; y=height-20*mm
        for line in txt.splitlines():
            if y < 20*mm: c.showPage(); y=height-20*mm
            c.drawString(x,y,line[:120]); y-=6*mm
        c.save(); return True
    except Exception:
        return False

def xpath_bool(root, expr: str) -> bool:
    if not HAVE_LXML: return False
    try:
        vals = root.xpath(expr)
        if isinstance(vals, list):
            return len(vals)>0 and (str(vals[0]).strip()!="" or hasattr(vals[0],"tag"))
        return bool(vals)
    except Exception:
        return False

def build_profile_purpose_section(root):
    cc = xtext(root,"//custom_class/text()")
    has_cc = xpath_bool(root,"//custom_class[text()]")
    method = (xtext(root,"/datawizardprofile/responsesettings/responseunits/unit_http/http_method/text()")
              or xtext(root,"/datawizardprofile/agent/http_method/text()") or "").upper()
    url = xtext(root,"/datawizardprofile/agent/url/text()")
    mime = (xtext(root,"/datawizardprofile/agent/mime_type/text()")
            or xtext(root,"/datawizardprofile/agent/content_type/text()") or "")
    if has_cc:
        overview = f"This profile extracts information using custom class '{cc}'."
    elif method or url:
        meth = method or "POST"
        overview = f"This profile receives {meth} requests{(' at '+url) if url else ''}{(' with '+mime) if mime else ''}."
    else:
        ptype = xtext(root,"/datawizardprofile/type/text()")
        overview = f"This profile is a scheduled or automated process ({ptype or 'unspecified'})."
    return {"name":"Profile Purpose","type":"kv","fields":[{"label":"Overview","value":overview}]}


# ---------- main ----------
def main():
    args = parse_args()
    xml_path = Path(args.xml); outdir = Path(args.outdir)
    mapping_path = Path(args.mapping); templates_dir = Path(args.templates)
    outdir.mkdir(exist_ok=True, parents=True)

    root = load_xml(xml_path)
    mapping = load_mapping(mapping_path)
    data = apply_mapping(root, mapping)
    data["source_file"] = xml_path.name  # shown on cover

    # Insert/refresh Profile Purpose at top
    purpose = build_profile_purpose_section(root)
    data["sections"] = [s for s in data["sections"] if s.get("name") not in ("Business Context","Profile Purpose")]
    data["sections"].insert(0, purpose)

    # Build workflow info
    info = extract_workflow_info(root)

    # Insert Input Data (3 points), replacing any existing "Input Data"
    data["sections"] = [s for s in data["sections"] if s.get("name") != "Input Data"]
    input_sec = build_input_data_section(root, info)
    # place it after Profile Purpose
    data["sections"].insert(1, input_sec)

    # Steps + diagram
    data["steps"] = build_steps(info)
    diagram_path = render_workflow_svg(info, outdir, xml_path.stem)
    data["workflow_link"] = ""
    if diagram_path:
        try:
            rel = Path(diagram_path).relative_to(outdir)
            data["workflow_link"] = str(rel).replace("\\", "/")
        except Exception:
            data["workflow_link"] = str(diagram_path)

    env = Environment(loader=FileSystemLoader(str(templates_dir)),
                      autoescape=select_autoescape(['html','xml']))

    # Ensure CSS sits next to the HTML (wkhtmltopdf resolves relative to the HTML file)
    html_path = outdir / f"{xml_path.stem}.html"
    pdf_path  = outdir / f"{xml_path.stem}.pdf"
    css_src = Path(STYLE_FILE)
    css_out = outdir / css_src.name
    try:
        if css_src.exists():
            css_out.write_bytes(css_src.read_bytes())
    except Exception:
        pass

    html = render_html(env, data, css_href=css_out.name)
    write_bytes(html_path, html.encode("utf-8"))

    # Try PDF generators
    if (try_pdf_from_html(html_path, pdf_path, args.wkhtmltopdf)
        or try_weasyprint(html_path, pdf_path)
        or try_reportlab_text_only(html_path, pdf_path)):
        print(f"[OK] Generated: {pdf_path.name}")
        sys.exit(0)

    print("[WARN] HTML created; PDF not generated.")
    sys.exit(1)


if __name__ == "__main__":
    main()
