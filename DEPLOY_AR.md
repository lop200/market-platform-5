# النشر على Render

1. اربط المستودع في Render واختر **Blueprint**.
2. سيقرأ Render ملف `render.yaml` وينشئ خدمة الويب وPostgreSQL.
3. أضف الأسرار: `API_KEY`, `ACCESS_CODE_MAIN`, `ALPACA_API_KEY`, `ALPACA_API_SECRET`, `OPENAI_API_KEY`.
4. اترك `MARKET_DATA_PROVIDER=alpaca`. استخدم `ALPACA_FEED=iex` للخطة المجانية أو `sip` عند وجود اشتراك.
5. يثبت البناء `requirements.txt`، ثم يشغل `alembic upgrade head` قبل Uvicorn.
6. تحقق من `/api/v1/health` ثم افتح الصفحة الرئيسية وابدأ مهمة مسح.
7. تحقق من `/results` للتأكد أن لوحة التدقيق تعمل من PostgreSQL.

لا تضف المفاتيح إلى GitHub أو إلى JavaScript. Finnhub للأخبار اختياري عبر `NEWS_PROVIDER=finnhub` و`FINNHUB_API_KEY`.

قيود البيانات:

- خلاصة Alpaca IEX لحظية ضمن تغطية IEX فقط وليست SIP كاملًا؛ الاشتراك في SIP مطلوب لتغطية السوق الكاملة.
- الأخبار تتطلب Finnhub عند تفعيل `NEWS_PROVIDER=finnhub`.
- yfinance مناسب للتطوير فقط وقد يغيب عنه Bid/Ask، وعندها يرفض حارس الجودة إنشاء نتيجة حية.
