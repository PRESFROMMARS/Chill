"""Pinned developer images shared by the desktop and the Windows packager."""
import hashlib
import http.client
import os
from pathlib import Path
import tempfile
import urllib.request


DDI_ROOT = Path("assets/ddi")
DDI_REVISION = "e2aaeac89ca8fa56c2fa1f144d182994e07a525b"
DDI_ASSETS = {
    "Personalized": {
        "BuildManifest.plist": (801505, "8edd4a2f4f4ef1fbd7bfe49785d8badc673d1395d1d94d85b132ca8ab5ecaf54"),
        "Image.dmg": (15733248, "05fd807da5e19f030fa4941f24800c965c6c77982ab572dd5d1ef778fb69f9ca"),
        "Image.dmg.trustcache": (1895, "36af60889ff5a737874a26daeb8e1a0139ebfebec6ec2e4d8f6a3c1bf1dce35c"),
    },
    "Cryptex": {
        "BuildManifest.plist": (804946, "27385d7582b03b36bb3104e22b520aee0c47d72fecb4e8ecfe12ef5d966c7012"),
        "Image.dmg": (15895040, "873097f695a8b9734e2abc54f795a8874d40ff6fd11208ecb01ef29534c7c176"),
        "Image.dmg.trustcache": (1895, "f7f21986074eee03a215aca16ecfc78d6bf183600d8a0d2fb691f9896782e6f0"),
        "Image.dmg.cryptex_info": (430, "edf49aef55aacc063d4d7be05b713bb545ce2993b3f62bcc15eccd75e610ee6c"),
        "Image.dmg.root_hash": (229, "3543fad2805b88119695c417e12679380b3b5a2742994bbcc839c8e2de5d7302"),
    },
}


def _matches(data: bytes, expected: tuple[int, str]) -> bool:
    size, sha256 = expected
    return len(data) == size and hashlib.sha256(data).hexdigest() == sha256


def _valid(path: Path, expected: tuple[int, str]) -> bool:
    try:
        with path.open("rb") as stream:
            return _matches(stream.read(expected[0]+1), expected)
    except OSError:
        return False


def image_files(directory: Path, variant: str, *, driver_cache: bool = False) -> dict[str, Path]:
    """Return paths only if the entire pinned image is present and valid."""
    paths = {name: directory / name for name in DDI_ASSETS[variant]}
    if driver_cache and variant == "Personalized":
        trustcache = "Image.dmg.trustcache"
        if not _valid(paths[trustcache], DDI_ASSETS[variant][trustcache]):
            paths[trustcache] = directory / "Image.trustcache"
    for name, path in paths.items():
        if not _valid(path, DDI_ASSETS[variant][name]):
            raise ValueError(f"Missing or corrupt DDI resource: {variant}/{name}")
    return paths


def _download(variant: str, name: str, expected: tuple[int, str]) -> bytes:
    repo = "doronz88/DeveloperDiskImage"
    path = f"PersonalizedImages/Xcode_iOS_DDI_{variant}/{name}"
    raw = f"https://raw.githubusercontent.com/{repo}/{DDI_REVISION}/{path}"
    urls = (
        raw,
        f"https://fastly.jsdelivr.net/gh/{repo}@{DDI_REVISION}/{path}",
        f"https://testingcf.jsdelivr.net/gh/{repo}@{DDI_REVISION}/{path}",
        f"https://mirror.ghproxy.com/{raw}",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    errors = []
    for url in urls:
        try:
            with opener.open(url, timeout=3) as response:
                data = response.read(expected[0]+1)
            if not _matches(data, expected):
                raise ValueError("image size or SHA-256 mismatch")
            return data
        except (OSError, ValueError, http.client.HTTPException) as error:
            errors.append(f"{url.split('/')[2]}: {error}")
    raise OSError(f"Could not download DDI {variant}/{name}. " + "; ".join(errors))


def prepare_image(directory: Path, variant: str) -> dict[str, Path]:
    """Populate a build or source cache with verified files, one atomic write at a time."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, expected in DDI_ASSETS[variant].items():
        path = directory / name
        if _valid(path, expected):
            continue
        data = _download(variant, name, expected)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".ddi-", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(data)
                stream.close()
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    return image_files(directory, variant)
