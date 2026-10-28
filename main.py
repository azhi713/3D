from pathlib import Path
import tkinter as tk
from tkinter import messagebox
from ui import RiskApp


if __name__ == '__main__':
    try:
        RiskApp(Path(__file__).resolve().parent).mainloop()
    except Exception as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('程序无法启动', '请检查数据文件或运行环境。\n'+str(exc), parent=root)
        root.destroy()
        raise
