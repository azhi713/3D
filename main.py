from pathlib import Path
import sys
import tkinter as tk
from tkinter import messagebox
from ui import RiskApp


if __name__ == '__main__':
    try:
        # In PyInstaller --onedir/--onefile the source __file__ can live
        # in an internal/extraction directory. Keep customer data beside the EXE.
        root = Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
        RiskApp(root).mainloop()
    except Exception as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('程序无法启动', '请检查数据文件或运行环境。\n'+str(exc), parent=root)
        root.destroy()
        raise

# V262-20261101-CUSTOMER-TRANSPORT
