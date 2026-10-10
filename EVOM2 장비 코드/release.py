"""
EVOM2_TEER 빌드 · 배포 도구 (개발 PC 전용. 표준 라이브러리 + PyInstaller)

  python release.py build             exe 만들기 -> 이 폴더 EVOM2_TEER.exe, 배포용 폴더 2_EVOM2_TEER.exe
  python release.py publish "메모"    build 후 git commit · 태그 v<APP_VERSION> · push,
                                      GitHub 릴리스를 만들고 EVOM2_TEER.exe 를 올림
                                      -> 각 PC 의 프로그램이 [업데이트 확인] 으로 받아 감

새 버전을 낼 때: EVOM2_TEER.py 의 APP_VERSION 을 올리고 (예: 1.0.0 -> 1.0.1) publish.
도중에 실패하면 고친 뒤 같은 명령을 다시 실행하면 이어서 한다 (릴리스는 exe 를 다 올린 뒤에 공개됨).
GitHub 로그인은 이 PC 의 Git Credential Manager 에 저장된 것을 쓴다 (git push 가 되는 상태면 됨).
토큰은 화면에 출력하지 않는다.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

CODE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(CODE)                                   # git 저장소 맨 위 폴더
SRC = os.path.join(CODE, "EVOM2_TEER.py")
DEPLOY = os.path.join(ROOT, "EVOM2_TEER 배포용 (이 폴더만 다른 PC로 복사)")
# 빌드는 이 사용자만 쓸 수 있는 폴더에서 (공용 임시 폴더는 다른 계정이 결과물을 바꿔 칠 수 있음)
WORK = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "evom2_build")
ASSET = "EVOM2_TEER.exe"


def app_info():
    with open(SRC, encoding="utf-8") as f:
        s = f.read()
    return (re.search(r'^APP_VERSION = "([^"]+)"', s, re.M).group(1),
            re.search(r'^GITHUB_REPO = "([^"]+)"', s, re.M).group(1))


def build():
    """PyInstaller one-file, 창 모드, 매번 처음부터(--clean). nidaqmx 는 패키지 정보(metadata)까지 넣어야
    exe 안에서 불러와진다. -> 만든 exe 의 바이트 (업로드는 이 바이트 그대로)"""
    shutil.rmtree(WORK, ignore_errors=True)
    meta = [a for m in ("nidaqmx", "nitypes", "hightime", "tzlocal", "deprecation", "numpy")
            for a in ("--copy-metadata", m)]
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
                    "--name", "EVOM2_TEER", "--collect-submodules", "nidaqmx", "--collect-submodules", "nitypes",
                    *meta, "--workpath", os.path.join(WORK, "build"), "--distpath", os.path.join(WORK, "dist"),
                    "--specpath", WORK, SRC], check=True)
    with open(os.path.join(WORK, "dist", ASSET), "rb") as f:
        data = f.read()
    for dst in (os.path.join(CODE, ASSET), os.path.join(DEPLOY, "2_EVOM2_TEER.exe")):
        if os.path.isdir(os.path.dirname(dst)):
            with open(dst, "wb") as f:
                f.write(data)
    print(f"빌드 완료: {os.path.join(CODE, ASSET)}  ({len(data):,} bytes, sha256 {hashlib.sha256(data).hexdigest()})")
    return data


def git(*args, capture=False):
    return subprocess.run(["git", "-C", ROOT, *args], check=True, text=True, encoding="utf-8",
                          capture_output=capture).stdout


def github_token():
    p = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                       text=True, capture_output=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    cred = dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)
    if not cred.get("password"):
        sys.exit("GitHub 로그인 정보가 없습니다. 이 저장소에서 git push 를 한 번 해서 로그인한 뒤 다시 하세요.")
    return cred["password"]


def api(method, url, token, body=None, ctype="application/json"):
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "User-Agent": "EVOM2-TEER-release", "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            raw = r.read()
            return json.loads(raw) if raw else None            # DELETE 는 빈 응답(204)
    except urllib.error.HTTPError as e:
        sys.exit(f"GitHub API 오류 {e.code} ({method} {url.split('?')[0]}): {e.read()[:300].decode(errors='replace')}")


def publish(note):
    ver, repo = app_info()
    tag = f"v{ver}"
    token = github_token()                                     # 로그인부터 확인 (push 하기 전에)
    rel = next((r for r in api("GET", f"https://api.github.com/repos/{repo}/releases?per_page=100", token)
                if r["tag_name"] == tag), None)                # 초안(draft)도 찾음
    if rel and not rel["draft"] and any(a["name"] == ASSET for a in rel["assets"]):
        sys.exit(f"{tag} 릴리스가 이미 있습니다. EVOM2_TEER.py 의 APP_VERSION 을 올린 뒤 다시 하세요.")
    out = git("ls-remote", "--tags", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}", capture=True)
    refs = {name: sha for sha, name in (line.split("\t") for line in out.splitlines() if "\t" in line)}
    remote_tag = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")     # 주석 태그는 ^{} 가 커밋
    if remote_tag and remote_tag != git("rev-parse", "HEAD", capture=True).strip():
        sys.exit(f"GitHub 의 {tag} 태그가 지금 커밋과 다릅니다. APP_VERSION 을 올려 새 버전으로 내세요.")
    if remote_tag and git("status", "--porcelain", capture=True).strip():
        sys.exit(f"{tag} 태그는 이미 올라갔는데 커밋하지 않은 수정이 있습니다. 그대로 이어서 내면 exe 와 태그의 코드가 "
                 "달라지므로, 수정을 되돌리거나 APP_VERSION 을 올려 새 버전으로 내세요.")
    data = build()                                             # 빌드가 실패하면 여기서 멈춤 (아무것도 안 올라감)
    if not remote_tag:
        git("add", "-A")
        if git("status", "--porcelain", capture=True).strip():
            git("commit", "-m", f"{tag}: {note}")
        git("tag", "-f", "-a", tag, "-m", note)                # 아직 GitHub 에 없으니 지금 커밋으로 다시 붙여도 안전
        git("push", "origin", "HEAD")
        git("push", "origin", tag)
    if not rel:
        rel = api("POST", f"https://api.github.com/repos/{repo}/releases", token,
                  {"tag_name": tag, "name": tag, "body": note, "draft": True})
    for a in rel["assets"]:                                    # 덜 올라간 exe 가 남아 있으면 지우고 다시
        if a["name"] == ASSET:
            api("DELETE", f"https://api.github.com/repos/{repo}/releases/assets/{a['id']}", token)
    up = api("POST", f"https://uploads.github.com/repos/{repo}/releases/{rel['id']}/assets?name={ASSET}",
             token, data, "application/vnd.microsoft.portable-executable")
    local = "sha256:" + hashlib.sha256(data).hexdigest()
    if up.get("digest") and up["digest"] != local:
        sys.exit(f"올라간 exe 의 SHA-256 이 다릅니다 ({up['digest']} != {local}). 릴리스는 초안으로 남겨 둡니다.")
    rel = api("PATCH", f"https://api.github.com/repos/{repo}/releases/{rel['id']}", token, {"draft": False})
    print(f"릴리스 완료: {rel['html_url']}  ({up['size']:,} bytes, {local})")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "build":
        build()
    elif len(sys.argv) >= 3 and sys.argv[1] == "publish":
        publish(" ".join(sys.argv[2:]))
    else:
        sys.exit(__doc__)
