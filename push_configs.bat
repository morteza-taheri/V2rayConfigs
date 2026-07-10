@echo off
cd /d "D:\PMC\Documents\PyCharm\encoder_tool.py\SubRename"

:: 1. اجرای تست کانفیگ‌ها
::python main.py

:: 2. ثبت تمام تغییرات محلی (از جمله فایل‌های وضعیت و خروجی)
git add .
git commit -m "Auto-update: %date% %time%"

:: 3. دریافت تغییرات سرور و ترکیب با تغییرات خودمان (Rebase)
:: اگر تغییری در سرور باشد، به راحتی روی کارهای ما سوار می‌شود
git pull origin main --rebase

:: 4. اگر در حین pull تداخلی پیش آمد، آن را فورس کن (اختیاری اما برای اتوماسیون خوب است)
git add .
git commit --no-edit || echo "No commit needed"

:: 5. ارسال به گیت‌هاب
git push origin main