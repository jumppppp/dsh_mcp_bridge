"""Read-only delivery of the bundled Skills; never installs on a caller's behalf."""
from pathlib import Path
import hashlib
import re

ROOT = Path(__file__).resolve().parents[1] / "resources" / "skills"
NAMES = ("dsh-mcp-bridge", "dsh-mcp-bridge-maintenance")

def catalog(name=None):
    if name is not None and name not in NAMES:
        raise ValueError("Unknown bundled Skill")
    rows=[]
    for key in NAMES:
        if name is not None and name != key:
            continue
        data=(ROOT/key/"SKILL.md").read_bytes()
        content=data.decode("utf-8")
        match=re.search(r"^description: (.+)$",content,re.M)
        row={"name":key,"description":match.group(1) if match else "", "sha256":hashlib.sha256(data).hexdigest(),
             "files":[{"path":key+"/SKILL.md","content":content}]}
        rows.append(row)
    return {"source":"bundled","skills":rows,"installation":{
        "mode":"caller-managed", "workspace_path":".agents/skills/<name>/SKILL.md",
        "instructions":"Review the returned Skill, then write its relative file under the caller-authorized workspace or configured Skills directory. Write UTF-8 content bytes without changing newlines when checking SHA256. This tool does not install or execute code and never chooses an absolute destination for you."}}
