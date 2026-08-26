"""Small standalone tool for selecting a face coordinate from an image.

Run it with ``python face.py path/to/image.png``. Click once to print the
original-image coordinate and close the window. The tool has no project
dependencies and is intentionally independent from the Gallery plugin.
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

from PIL import Image, ImageTk


def main() -> None:
    """Open an image, print the original coordinate of the next click, and exit."""
    root = tk.Tk()
    root.withdraw()
    image_path = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(
            filedialog.askopenfilename(
                title="Select an image",
                filetypes=[
                    ("Image files", "*.png *.jpg *.jpeg *.webp *.gif"),
                    ("All files", "*.*"),
                ],
                parent=root,
            )
        )
    )
    if not image_path.is_file():
        root.destroy()
        return
    try:
        with Image.open(image_path) as source:
            image = source.convert("RGB")
    except OSError as error:
        messagebox.showerror("Unable to open image", str(error))
        root.destroy()
        return

    root.deiconify()
    root.title(f"Select face coordinate - {image_path.name}")
    max_width, max_height = 1200, 800
    scale = min(max_width / image.width, max_height / image.height, 1.0)
    display_size = (
        max(1, round(image.width * scale)),
        max(1, round(image.height * scale)),
    )
    displayed = image.resize(display_size, Image.Resampling.LANCZOS)
    photo = ImageTk.PhotoImage(displayed)
    canvas = tk.Canvas(root, width=display_size[0], height=display_size[1])
    canvas.pack()
    canvas.create_image(0, 0, anchor=tk.NW, image=photo)

    def select(event: tk.Event[tk.Misc]) -> None:
        """Convert the display coordinate to an original-image coordinate."""
        x = min(max(event.x / scale, 0), image.width - 1)
        y = min(max(event.y / scale, 0), image.height - 1)
        print(f"({x:.0f}, {y:.0f})")
        root.destroy()

    canvas.bind("<Button-1>", select)
    root.mainloop()


if __name__ == "__main__":
    main()
