"""V2-local launch defaults shared by the panel, prompt generator and MCP."""
import json
import os
from pathlib import Path
import threading
import uuid
from .platform import locations, private_directory
from .policy import KNOWN_PERMISSION_PRESETS, workspace_path

LOCK=threading.RLock()
FIELDS={"provider","model","effort","preset","permission"}
BUILTIN={"provider":"local","model":"deepseek-v4-flash","permission":"workspace-write","preset":"standard"}

def program_workspace():
    path=Path(__file__).resolve().parents[2]/"workspace"
    path.mkdir(exist_ok=True)
    return path

def default_workspace(data=None):
    selected=(data or {}).get("default_workspace")
    return str(program_workspace()) if not selected or selected=="@program" else selected

def _path(state=None):
    return Path(state or locations().state)/"workspace_defaults.json"

def load(state=None):
    path=_path(state)
    if not path.exists():
        return {"default_workspace":"","workspaces":{}}
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,ValueError) as error:
        raise ValueError("Workspace defaults cannot be read; preserve the file and repair it") from error
    if not isinstance(value,dict) or not isinstance(value.get("workspaces"),dict):
        raise ValueError("Invalid workspace defaults")
    if "@program" in value["workspaces"]:
        value["workspaces"]["@program"]["cwd"]=str(program_workspace())
    return value

def _key(path):
    if str(path)=="@program":
        return "@program"
    key=os.path.normcase(str(Path(path).resolve()))
    return "@program" if key==os.path.normcase(str(program_workspace().resolve())) else key

def snapshot(cwd=None,state=None):
    data=load(state)
    if cwd:
        row=data["workspaces"].get(_key(cwd),{})
        return {"cwd":str(Path(cwd).resolve()),"settings":{**BUILTIN,**row.get("settings",{})},
                "stored":row.get("settings",{}),"is_default":_key(cwd)==_key(default_workspace(data)),
                "model_selection":"require-effective-match; no global default mutation"}
    return {**data,"default_workspace":default_workspace(data),"program_workspace":str(program_workspace()),"workspace_mode":"program-relative" if not data.get("default_workspace") or data.get("default_workspace")=="@program" else "explicit", "builtin":BUILTIN.copy(),"model_selection":"require-effective-match; no global default mutation"}

def save(cwd,settings,make_default=None,state=None):
    cwd=str(workspace_path(cwd))
    if not isinstance(settings,dict) or set(settings)-FIELDS:
        raise ValueError("Unsupported workspace default field")
    cleaned={}
    for key,value in settings.items():
        if not isinstance(value,str) or len(value)>200:
            raise ValueError("Default values must be short strings")
        if value.strip():
            cleaned[key]=value.strip()
    if cleaned.get("permission","workspace-write") not in KNOWN_PERMISSION_PRESETS:
        raise ValueError("Unsupported permission")
    if bool(cleaned.get("provider")) != bool(cleaned.get("model")):
        raise ValueError("Model and provider must be selected together")
    if make_default is not None and not isinstance(make_default,bool):
        raise ValueError("make_default must be boolean")
    with LOCK:
        data=load(state)
        data["workspaces"][_key(cwd)]={"cwd":cwd,"settings":cleaned}
        if make_default is True:
            data["default_workspace"]="@program" if _key(cwd)==_key(program_workspace()) else cwd
        elif make_default is False and _key(data.get("default_workspace") or ".")==_key(cwd):
            data["default_workspace"]=""
        path=_path(state)
        private_directory(path.parent)
        temporary=path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
        try:
            temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
            os.replace(temporary,path)
        finally:
            temporary.unlink(missing_ok=True)
    return snapshot(cwd,state)

def resolve(params,state=None):
    result=dict(params)
    data=load(state)
    cwd=result.get("cwd") or default_workspace(data)
    cwd=str(workspace_path(cwd))
    result["cwd"]=cwd
    settings={**BUILTIN,**data["workspaces"].get(_key(cwd),{}).get("settings",{})}
    # Choosing either model field explicitly must not combine it with a stale
    # provider/model from workspace defaults.
    if result.get("model") or result.get("provider"):
        settings.pop("model",None)
        settings.pop("provider",None)
    for key,value in settings.items():
        if not result.get(key):
            result[key]=value
    return result
