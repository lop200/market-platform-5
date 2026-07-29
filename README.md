# مرصاد الأسهم

منصة عربية داكنة لاكتشاف فرص مشروطة في الأسهم الأمريكية بين 2 و5 دولارات. الصفحة تعرض آخر نتائج محفوظة فورًا، بينما يعمل المسح والاتصالات الخارجية في الخلفية.

## ما توفره المنصة

- مسح خلفي لا يحبس تحميل الصفحة، مع تقدم وعدد الأسهم المفحوصة والمستبعدة.
- حارس جودة يرفض Bid/Ask الناقص، السعر القديم، والسبريد غير المقبول.
- مؤشرات واستراتيجيات ومستويات دخول وإبطال وأهداف وحجم صفقة محسوبة حتميًا في Python.
- مراجعة OpenAI مهيكلة لأفضل المرشحين فقط، مع Cost Gate ومنع إعادة التحليل عند ثبات بصمة البيانات.
- بحث محلي سريع بالرمز أو اسم الشركة، غير حساس لحالة الأحرف؛ مثل `nv` أو `NVIDIA`.
- صفحة `/results` للنتائج المسجلة فعليًا حسب الاستراتيجية والسوق والجلسة.
- انتهاء تلقائي للتحليلات القديمة وتدقيق زمني للجلسة وبعد يوم ويومين.

## تشغيل محلي

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python -m alembic upgrade head
.\.venv\Scripts\python -m uvicorn app.main:app --reload
```

افتح `http://127.0.0.1:8000`.

## المزودات

- `MARKET_DATA_PROVIDER=alpaca`: موصى به للإنتاج ويقدم Bid/Ask. خلاصة IEX محدودة مقارنة بـSIP.
- `MARKET_DATA_PROVIDER=yfinance`: تطوير محلي، بيانات غير رسمية ومتأخرة وقد يغيب Bid/Ask، لذلك قد يرفض حارس الجودة الفرصة.
- `NEWS_PROVIDER=finnhub`: أخبار اختيارية. عند غيابها يظهر ذلك صراحة.

قائمة الإكمال التلقائي محلية ومنتقاة لتسريع الإدخال، وليست قائمة إدراج رسمية أو بديلًا عن Universe مزود السوق.

## الإعدادات

راجع `.env.example` للقائمة الكاملة. أهمها:

- `STOCK_MIN_PRICE`, `STOCK_MAX_PRICE`
- `MIN_AVG_DAILY_VOLUME`, `MIN_RELATIVE_VOLUME`, `MAX_SPREAD_PCT`
- `MIN_RISK_REWARD`, `MAX_QUOTE_AGE_SECONDS`, `MAX_RESULTS`
- `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_TIMEOUT_SECONDS`, `OPENAI_MAX_RETRIES`, `OPENAI_DAILY_BUDGET_USD`
- `DEFAULT_CAPITAL_SAR`, `DEFAULT_RISK_PCT`, `USD_SAR_RATE`

## API

- `POST /api/v1/opportunities/scans`
- `GET /api/v1/opportunities/scans/{run_id}`
- `GET /api/v1/opportunities/latest`
- `POST /api/v1/opportunities/symbols/{symbol}`
- `POST /api/v1/opportunities/{id}/refresh`
- `GET /api/v1/opportunities/results/summary`
- `GET|PUT /api/v1/opportunities/risk-settings`

## اختبار ونشر

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m alembic upgrade head
```

تفاصيل Render في `DEPLOY_AR.md`. جميع الاستدعاءات تتم من الخادم، ولا تُحفظ مفاتيح API في قاعدة البيانات.

تحتفظ سلسلة Alembic التاريخية بأسماء migrations القديمة لضمان ترقية قواعد البيانات القائمة بأمان، لكن migration منصة الأسهم تحذف جداول المشتقات، ولا توجد لها Models أو Routes أو Templates في التطبيق التشغيلي.
