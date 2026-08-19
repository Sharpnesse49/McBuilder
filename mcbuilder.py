import sys, json, platform, zipfile, threading, os, time
from pathlib import Path
from urllib.request import urlretrieve, urlopen
from concurrent.futures import ThreadPoolExecutor

MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
RESOURCES = "https://resources.download.minecraft.net"

if platform.system() == "Windows":
    os.system("")  # enable ANSI colors on legacy cmd.exe

RESET, BOLD, DIM, ITALIC = "\033[0m", "\033[1m", "\033[2m", "\033[3m"
GREEN = "\033[32m"
RED = "\033[31m"
SEP = "=" * 45

def log(msg):
    print(f"  {ITALIC}{msg}{RESET}")

def ok(msg):
    print(f"  {GREEN}\u2713 {msg}{RESET}")

def err(msg):
    print(f"  {RED}\u2717 {msg}{RESET}")

def print_bar(done, total, width=30):
    pct = done / total if total else 1
    filled = int(width * pct)
    bar = f"{BOLD}{chr(0x2588) * filled}{RESET}{DIM}{chr(0x2591) * (width - filled)}{RESET}"
    print(f"\r  {bar} {BOLD}{pct * 100:5.1f}%{RESET}", end="", flush=True)

def clear_bar():
    print("\r\033[K", end="")

def fetch_json(url):
    with urlopen(url) as r:
        return json.load(r)

def os_name():
    return {"Linux": "linux", "Windows": "windows", "Darwin": "osx"}.get(platform.system(), "linux")

def is_allowed(item, osn):
    rules = item.get("rules")
    if not rules:
        return True
    allow = False
    for r in rules:
        match = "os" not in r or r["os"].get("name") == osn
        if "features" in r:
            match = False
        if match:
            allow = r.get("action", "allow") == "allow"
    return allow

def get_args(arg_list, osn):
    res = []
    for arg in arg_list:
        if isinstance(arg, str):
            res.append(arg)
        elif is_allowed(arg, osn):
            val = arg["value"]
            res.extend(val if isinstance(val, list) else [val])
    return res

def main():
    print(f"""{BOLD}
    __  ___     ____        _ __    __         
   /  |/  /____/ __ )__  __(_) /___/ /__  _____
  / /|_/ / ___/ __  / / / / / / __  / _ \\/ ___/
 / /  / / /__/ /_/ / /_/ / / / /_/ /  __/ /    
/_/  /_/\\___/_____/\\__,_/_/_/\\__,_/\\___/_/     
{RESET}{SEP}
""")

    version = input("  minecraft version: ").strip()
    if not version:
        sys.exit(1)

    instance_name = input(f"  instance name (default: {version}): ").strip() or version
    print()

    start_time = time.time()

    log("fetching version manifest...")
    manifest = fetch_json(MANIFEST_URL)
    entry = next((v for v in manifest["versions"] if v["id"] == version), None)
    if not entry:
        err(f"version {version!r} not found")
        sys.exit(1)
    vjson = fetch_json(entry["url"])
    ok(f"{version} found!")

    d = Path(instance_name).resolve()
    vdir = d / "versions" / version
    for folder in [d / "libraries", d / "assets" / "indexes", d / "assets" / "objects", d / "natives", vdir]:
        folder.mkdir(parents=True, exist_ok=True)

    downloads = [(vjson["downloads"]["client"]["url"], vdir / f"{version}.jar")]
    osn = os_name()
    classpath = []
    native_jars = []

    for lib in vjson.get("libraries", []):
        if not is_allowed(lib, osn):
            continue
        art = lib.get("downloads", {}).get("artifact")
        if art:
            downloads.append((art["url"], d / "libraries" / art["path"]))
            classpath.append(f"libraries/{art['path']}")
        nkey = lib.get("natives", {}).get(osn)
        if nkey:
            nkey = nkey.replace("${arch}", "64")
            classifier = lib.get("downloads", {}).get("classifiers", {}).get(nkey)
            if classifier:
                njar = d / "libraries" / classifier["path"]
                downloads.append((classifier["url"], njar))
                native_jars.append(njar)

    classpath.append(f"versions/{version}/{version}.jar")

    aid = "legacy"
    aindex = vjson.get("assetIndex")
    if aindex:
        aid = aindex["id"]
        objects = fetch_json(aindex["url"])
        (d / "assets" / "indexes" / f"{aid}.json").write_text(json.dumps(objects))
        for h in {o["hash"] for o in objects["objects"].values()}:
            sub = h[:2]
            downloads.append((f"{RESOURCES}/{sub}/{h}", d / "assets" / "objects" / sub / h))

    total = len(downloads)
    done = 0
    lock = threading.Lock()

    def dl(task):
        nonlocal done
        url, dest = task
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            try:
                urlretrieve(url, dest)
            except Exception:
                pass
        with lock:
            done += 1
            print_bar(done, total)

    log(f"downloading {total} files...")
    with ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(dl, downloads))
    clear_bar()

    for njar in native_jars:
        if njar.exists():
            with zipfile.ZipFile(njar) as z:
                for name in z.namelist():
                    if not name.startswith("META-INF"):
                        z.extract(name, d / "natives")

    ok(f"{version} downloaded!")

    log("generating start.py...")
    jvm_args = get_args(vjson.get("arguments", {}).get("jvm", []), osn)
    game_args = get_args(vjson.get("arguments", {}).get("game", []), osn)
    if "minecraftArguments" in vjson:
        game_args = vjson["minecraftArguments"].split()
    if not jvm_args:
        jvm_args = ["-Djava.library.path=${natives_directory}", "-cp", "${classpath}"]

    launch = {
        "main_class": vjson["mainClass"],
        "classpath": classpath,
        "jvm_args": jvm_args,
        "game_args": game_args,
        "version": version,
        "asset_index": aid,
    }
    (d / "launch.json").write_text(json.dumps(launch, indent=2))
    (d / "start.py").write_text(START_PY)
    ok("start.py ready!")

    elapsed = round(time.time() - start_time)
    print(f"\n  {BOLD}done!{RESET} {DIM}(in {elapsed}s){RESET}")
    print(f"  {DIM}now run start.py (java must be on PATH){RESET}")
    print(SEP)
    print()

START_PY = 'import os, json, subprocess, uuid\nfrom pathlib import Path\n\nUSERNAME = "Player"  # <- change this to your name\n\ndef main():\n    base = Path(__file__).parent.resolve()\n    launch = json.loads((base / "launch.json").read_text())\n\n    classpath = os.pathsep.join(str(base / p) for p in launch["classpath"])\n    values = {\n        "${auth_player_name}": USERNAME,\n        "${version_name}": launch["version"],\n        "${game_directory}": str(base),\n        "${assets_root}": str(base / "assets"),\n        "${assets_index_name}": launch["asset_index"],\n        "${auth_uuid}": str(uuid.uuid4()),\n        "${auth_access_token}": "0",\n        "${user_type}": "legacy",\n        "${version_type}": "release",\n        "${classpath}": classpath,\n        "${natives_directory}": str(base / "natives"),\n        "${library_directory}": str(base / "libraries"),\n        "${classpath_separator}": os.pathsep,\n    }\n\n    def resolve(args):\n        for k, v in values.items():\n            args = [a.replace(k, v) for a in args]\n        return args\n\n    os.chdir(base)\n    cmd = ["java", "-Xms1G", "-Xmx2G", *resolve(launch["jvm_args"]),\n           launch["main_class"], *resolve(launch["game_args"])]\n    subprocess.run(cmd)\n\nif __name__ == "__main__":\n    main()\n'

if __name__ == "__main__":
    main()
