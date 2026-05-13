# Uploader — אפליקציית macOS/Windows לעובדות

אפליקציה שקופה שרצה ברקע על המחשב של העובדת. כל 30 דקות סורקת את תיקיית הקלטות הזום, מעלה הקלטות חדשות לשרת המרכזי שמתמלל ומסכם אותן ויוצר Note ב-GHL.

> **הוראות התקנה לעובדות עצמן** — ראה את ה-README הראשי / מסמך הסיוע (יבוא בשלב הבא).
>
> המסמך הזה הוא **לפיתוח ובנייה**.

## פיתוח מקומי (CLI בלבד, בלי GUI)

```bash
brew install python-tk@3.12  # ל-Tkinter, פעם אחת
cd uploader
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# הרצה ראשונית: הגדרה
python -m src.cli init       # שאלות אינטראקטיביות
python -m src.cli test-connection
python -m src.cli scan-once  # סריקה ידנית
python -m src.cli run        # לופ אינסופי, Ctrl+C לעצירה
```

## הרצה עם GUI (אייקון בסרגל המנו)

```bash
python -m src.tray_app
```

נטען אייקון Z כחול בסרגל. תפריט הקליק-ימני (או הלחיצה הרגילה ב-Mac):
- סטטוס + סריקה אחרונה
- השהה / המשך
- סרוק עכשיו
- הגדרות… (פותח חלון Tkinter)
- פתח לוג
- יציאה

## בנייה ל-macOS (`.app` + `.dmg`)

```bash
cd uploader
bash build_macos.sh
```

תוצרים ב-`dist/`:
- `ZoomGHL.app` (22MB)
- `ZoomGHL.dmg` (~9MB, מכיל את ה-.app עם קישור ל-/Applications)

הסקריפט:
1. יוצר venv אם אין
2. מתקין `requirements-build.txt` (כולל pyinstaller)
3. מריץ `build_icon.py` שמפיק `.icns` + `.ico` + `.png`
4. PyInstaller לפי `zghl.spec`
5. hdiutil בונה DMG עם קישור ל-Applications

האפליקציה מסומנת `LSUIElement=True` ב-Info.plist — לא תופיע ב-Dock, רק בסרגל המנו.

## בנייה ל-Windows (`.exe`)

מתוך מכונת Windows עם Python 3.12 מותקן:
```cmd
cd uploader
build_windows.bat
```

תוצרים ב-`dist\ZoomGHL\`. ה-`.exe` עומד בעצמו אבל מצריך את שאר הקבצים בתיקייה. **לאריזה כ-installer**: השתמש ב-Inno Setup או NSIS (לא כלול כאן).

> בנייה ל-Windows חייבת לרוץ על Windows. PyInstaller לא תומך ב-cross-compile.

## מבנה הקוד

```
uploader/
├── main.py                  ← Entry point ל-PyInstaller (stub שטוען src.tray_app)
├── src/
│   ├── __init__.py          ← APP_NAME, __version__
│   ├── paths.py             ← config_path/local_db_path/log_path פר OS
│   ├── config.py            ← Config dataclass + JSON I/O
│   ├── db.py                ← SQLite מקומי (uploads table)
│   ├── watcher.py           ← Zoom folder scanning + idle filter
│   ├── uploader.py          ← POST /upload + move ל-uploaded/
│   ├── service.py           ← ScannerService — thread רקע
│   ├── icon.py              ← מחולל אייקון פרוצדורלית עם PIL
│   ├── settings_window.py   ← Tkinter window (תהליך משנה)
│   ├── tray_app.py          ← pystray entrypoint
│   └── cli.py               ← typer CLI מקבילי (לפיתוח/דיאגנוסטיקה)
├── build_assets/             ← נכסים שנוצרים (icon.icns/.ico/.png)
├── build_macos.sh
├── build_windows.bat
├── build_icon.py
├── zghl.spec                ← PyInstaller spec
├── requirements.txt
└── requirements-build.txt   ← runtime + pyinstaller
```

## נתיבים שהאפליקציה משתמשת בהם

|  | macOS | Windows |
|---|---|---|
| Config + DB + לוג | `~/Library/Application Support/ZoomGHL/` | `%APPDATA%\ZoomGHL\` |
| Zoom recordings (ברירת מחדל) | `~/Documents/Zoom/` | `%USERPROFILE%\Documents\Zoom\` |
| תיקייה להעלאות שהושלמו | `<watch>/uploaded/` | `<watch>\uploaded\` |
