# مرصاد الأسهم

منصة عربية داكنة لتحليل أي سهم أمريكي صحيح، مع ماسح سوق اختياري النطاق لاكتشاف قراءات مشروطة. الصفحة تعرض آخر نتائج محفوظة فورًا، بينما يعمل المسح والاتصالات الخارجية في الخلفية.

## ما توفره المنصة

- مسح خلفي لا يحبس تحميل الصفحة، مع تقدم وعدد الأسهم المفحوصة والمستبعدة.
- حارس جودة تنفيذي يرفض Bid/Ask الناقص أو غير المتزامن، السعر/الشموع القديمة، السوق المغلق، والسبريد غير المقبول؛ وعند الرفض لا تُنشأ خطة أو احتمالات أو انحياز اتجاهي.
- مؤشرات واستراتيجيات ومستويات دخول وإبطال وأهداف وحجم صفقة محسوبة حتميًا في Python.
- تسلسل إلزامي: تحليل السهم ← فرصة سهم صالحة ← OPRA Option Chain ← تصفية ← ترتيب ← مراجعة OpenAI.
- صائد Call وPut للشركات مع Strike وDTE وBid/Ask وGreeks وIV والسيولة والتكلفة وBreak-even والأهداف والوقف؛ Paper Trading فقط.
- تعطيل كامل وآمن للخيارات عبر `OPTIONS_ENABLED=false` دون التأثير على تحليل الأسهم.
- تقويم أرباح وأخبار محفوظة تُحدّث في الخلفية، مع تحذيرات IV Crush وفجوات الأرباح.
- مراجعة OpenAI مهيكلة لأفضل المرشحين فقط، مع Cost Gate ومنع إعادة التحليل عند ثبات بصمة البيانات.
- بحث محلي سريع بالرمز أو اسم الشركة، غير حساس لحالة الأحرف؛ مثل `nv` أو `NVIDIA`.
- صفحة مستقلة للسهم المحدد لا تطبق فلتر سعر الماسح، وتعرض شموعًا تفاعلية 1m و5m و15m و1h واليومي مع Crosshair وZoom/Pan.
- الماسح يبدأ افتراضيًا بـ«جميع الأسعار» ولا يطبق قيدًا سعريًا عامًا. الفلاتر الاختيارية: أقل من 5، 5–20، 20–100، أكثر من 100، أو نطاق مخصص.
- Universe المسح يأتي من الأسهم الأمريكية النشطة لدى المزود؛ يجري Batch خفيفًا للترتيب، ثم تحليلًا عميقًا لأفضل 10، ويرسل خمسة كحد أقصى إلى OpenAI.
- عدادات المسح تفصل بين البيانات المجلوبة والناقصة والمتجاوزة رقميًا والمستبعدة فنيًا والمرشحين والنتائج النهائية.
- قياس طلبات API الفعلية وcache hits واستدعاءات OpenAI وتكلفتها وزمن الاستجابة.
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

- `MARKET_DATA_PROVIDER=alpaca`: موصى به للإنتاج، مع `ALPACA_DATA_FEED=sip` للأسهم و`ALPACA_OVERNIGHT_FEED=boats` للتداول الليلي.
- `ALPACA_OPTIONS_FEED=opra`: بيانات عقود الشركات عند توفر صلاحية OPRA في الحساب.
- `MARKET_DATA_PROVIDER=yfinance`: تطوير محلي، بيانات غير رسمية ومتأخرة وقد يغيب Bid/Ask، لذلك قد يرفض حارس الجودة الفرصة.
- `NEWS_PROVIDER=finnhub`: أخبار اختيارية. عند غيابها يظهر ذلك صراحة.

قائمة الإكمال التلقائي محلية ومنتقاة لتسريع الإدخال، وليست قائمة إدراج رسمية أو بديلًا عن Universe مزود السوق.

## الإعدادات

راجع `.env.example` للقائمة الكاملة. أهمها:

- `MIN_AVG_DAILY_VOLUME`, `MIN_RELATIVE_VOLUME`, `MAX_SPREAD_PCT`
- `MIN_RISK_REWARD`, `MAX_QUOTE_AGE_SECONDS`, `MAX_RESULTS`
- `MAX_QUOTE_TIMESTAMP_SKEW_SECONDS`, `MAX_CANDLE_AGE_SECONDS`, `MAX_QUOTE_CANDLE_SKEW_SECONDS`
- `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_TIMEOUT_SECONDS`, `OPENAI_MAX_RETRIES`, `OPENAI_DAILY_BUDGET_USD`
- `OPTIONS_ENABLED`, `ALPACA_OPTIONS_FEED`, `OPTIONS_MIN_DTE`, `OPTIONS_MAX_DTE`
- `OPTIONS_MAX_SPREAD_PCT`, `OPTIONS_MIN_VOLUME`, `OPTIONS_MIN_OPEN_INTEREST`, `OPTIONS_MAX_QUOTE_AGE_SECONDS`
- `OPTIONS_MIN_ABS_DELTA`, `OPTIONS_MAX_ABS_DELTA`, `OPTIONS_MAX_OTM_PCT`
- `OPTIONS_MAX_CAPITAL_PCT`, `OPTIONS_MAX_PREMIUM_LOSS_PCT`, `OPTIONS_EARNINGS_RISK_DAYS`
- `EARNINGS_PROVIDER`, `EARNINGS_CACHE_SECONDS`, `EARNINGS_TODAY_CACHE_SECONDS`, `EARNINGS_CALENDAR_LIMIT`, `EARNINGS_ENRICHMENT_LIMIT`, `EARNINGS_REVIEW_DAYS`, `EARNINGS_NO_NEW_ENTRY_DAYS`, `FINNHUB_API_KEY`
- `DEFAULT_CAPITAL_SAR`, `DEFAULT_RISK_PCT`, `USD_SAR_RATE`

## API

- `POST /api/v1/opportunities/scans`
- `GET /api/v1/opportunities/scans/{run_id}`
- `GET /api/v1/opportunities/latest`
- `POST /api/v1/opportunities/symbols/{symbol}`
- `GET /api/v1/opportunities/stocks/jobs/{run_id}`
- `GET /stocks/{symbol}`
- `POST /api/v1/opportunities/{id}/refresh`
- `GET /api/v1/opportunities/results/summary`
- `GET|PUT /api/v1/opportunities/risk-settings`
- `GET /api/v1/dashboard`
- `POST /api/v1/dashboard/refresh-events`

## اختبار ونشر

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m alembic upgrade head
```

تفاصيل Render في `DEPLOY_AR.md`. جميع الاستدعاءات تتم من الخادم، ولا تُحفظ مفاتيح API في قاعدة البيانات.

تحتفظ سلسلة Alembic التاريخية بأسماء migrations القديمة لضمان ترقية قواعد البيانات القائمة بأمان. دعم عقود الشركات الحالي معزول خلف `OPTIONS_ENABLED` ويمنع التنفيذ الحقيقي التلقائي.
