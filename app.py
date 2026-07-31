"""旧ビルド設定との互換用起動ファイル。

GitHub Actionsやbuild_windows.batが app.py を指定していても、
実際の起動処理は main.py、GUIは gui.py、実行制御は controller.py が担当します。
"""
from main import main


if __name__ == "__main__":
    main()
