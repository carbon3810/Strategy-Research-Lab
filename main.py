from __future__ import annotations

import traceback
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

from core import app_dir
from gui import StrategyResearchLabApp


def write_startup_error(error_text: str) -> None:
    try:
        (app_dir() / "startup_error.log").write_text(error_text, encoding="utf-8")
    except Exception:
        pass


def main() -> None:
    try:
        app = StrategyResearchLabApp()
        app.mainloop()
    except Exception:
        error_text = traceback.format_exc()
        write_startup_error(error_text)
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "起動エラー",
                "Strategy Research Labの起動に失敗しました。\n\n"
                "EXEと同じフォルダの startup_error.log を確認してください。\n\n"
                + error_text[-1600:],
            )
            root.destroy()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
