import platform
import subprocess
import sys
from pathlib import Path

OS = platform.system()
HERE = Path(__file__).resolve().parent

MIN_PY = (3, 11)
MAX_PY = (3, 13)


def _run(label: str, args: list[str]) -> None:
    print(f"\n▶ {label}")
    subprocess.run(args, check=True)


def _check_python() -> None:
    v = sys.version_info[:2]
    if v > MAX_PY:
        print(f"\n⚠️  Python {v[0]}.{v[1]} is newer than the "
              f"{MAX_PY[0]}.{MAX_PY[1]} this is tested on. Continuing — if a "
              f"package has no wheel yet, install Python "
              f"{MAX_PY[0]}.{MAX_PY[1]} and run setup with that.")
        return
    if v < MIN_PY:
        print(f"\n❌ Python {v[0]}.{v[1]} detected — needs at "
              f"least Python {MIN_PY[0]}.{MIN_PY[1]}.")
        print("   Install a supported version and run setup with it, e.g.:")
        print(f"     py -{MIN_PY[0]}.{MIN_PY[1]} setup.py        (Windows)")
        print(f"     python{MIN_PY[0]}.{MIN_PY[1]} setup.py      (macOS / Linux)")
        sys.exit(1)


def _check_assets() -> None:
    face = HERE / "core" / "face_model.obj"
    if not face.exists() or face.stat().st_size < 4096:
        print(
            "\n⚠️  core/face_model.obj is missing or truncated — the avatar will "
            "fall back to the plain glowing core.\n"
            "    Re-clone the repository, or fetch that one file again."
        )


def main() -> None:
    print(f"⚙  Setup — detected OS: {OS or 'unknown'}, "
          f"Python {sys.version_info[0]}.{sys.version_info[1]}")
    _check_python()

    _run("Installing Python dependencies (OS-specific extras auto-filtered)…",
         [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])

    try:
        _run("Installing Playwright browsers (chromium + firefox)…",
             [sys.executable, "-m", "playwright", "install", "chromium", "firefox"])
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"\n⚠️  Playwright browsers were not installed ({e}).")
        print("    Everything except browser automation works. Retry later with:")
        print(f'    {sys.executable} -m playwright install chromium firefox')

    _check_assets()

    if OS == "Windows":
        try:
            import win32com.client
        except ImportError:
            postinstall = Path(sys.executable).parent / "Scripts" / "pywin32_postinstall.py"
            print(
                "\n⚠️  pywin32 did not register correctly — desktop-shortcut "
                "creation will use a slower fallback. To fix it, run:\n"
                f'    "{sys.executable}" -m pip install --force-reinstall pywin32\n'
                f'    "{sys.executable}" "{postinstall}" -install'
            )
    elif OS == "Linux":
        print(
            "\nℹ️  Linux note — a few voice-controlled OS actions shell out to "
            "native tools. Install the ones you'll use via your package manager:\n"
            "    • volume      → pulseaudio-utils   (pactl)\n"
            "    • brightness  → brightnessctl\n"
            "    • reminders   → systemd (systemd-run) or 'at'\n"
            "    • open URLs   → xdg-utils          (xdg-open)"
        )
    elif OS == "Darwin":
        print(
            "\nℹ️  macOS note — volume, brightness and reminders use the built-in "
            "'osascript' / LaunchAgents, so no extra tools are required.\n"
            "    For Safari automation only: python -m playwright install webkit"
        )

    print("\n✅ Setup complete!")
    print("   1) Launch it:  python main.py")
    print("   2) Paste your free Gemini API key when the setup screen appears.")
    print("   3) (Optional) Enable 'Hey Jarvis' from ⚙ → WAKE WORD.")


if __name__ == "__main__":
    main()
