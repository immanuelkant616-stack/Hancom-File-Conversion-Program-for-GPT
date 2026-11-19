"""Launch TR as a Windows GUI application."""

import faulthandler
import os
import sys
import traceback
from ctypes import windll
from pathlib import Path


def main():
    local_data = os.environ.get("LOCALAPPDATA")
    local_folder = Path(local_data) if local_data else Path.home() / "AppData" / "Local"
    log_path = local_folder / "TR" / "startup.log"
    original_stderr = sys.stderr
    log = None
    try:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = log_path.open("a", encoding="utf-8", buffering=1)
        except OSError:
            pass
        if log is not None:
            sys.stderr = log
            faulthandler.enable(file=log)

        from tr_gui import main as run_app

        run_app()
        return 0
    except Exception as error:
        message = f"TR을 시작하지 못했습니다.\n\n{error}"
        if log is not None:
            traceback.print_exc(file=log)
            message += f"\n\n오류 기록\n{log_path}"
        windll.user32.MessageBoxW(None, message, "TR", 0x10)
        return 1
    finally:
        if log is not None:
            faulthandler.disable()
            sys.stderr = original_stderr
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
