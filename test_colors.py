import tkinter as tk

import colors


def wink(color_idx):
    if color_idx < len(colors.ALL_COLORS):
        canvas.itemconfig(id_, fill=colors.ALL_COLORS[color_idx])
        canvas.after(500, wink, color_idx+1)


root = tk.Tk()
root.geometry('200x200')
canvas = tk.Canvas(root)
canvas.pack(fill=tk.BOTH, expand=1)
id_ = canvas.create_rectangle(0, 0, 200, 200)
canvas.after(500, wink, 0)
root.mainloop()
