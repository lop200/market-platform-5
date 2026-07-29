# مرصاد الأسهم

منصة عربية داكنة لاكتشاف فرص مشروطة في الأسهم الأمريكية بين 2 و5 دولارات. الصفحة تعرض آخر نتائج محفوظة فورًا، بينما يعمل المسح والاتصالات الخارجية في الخلفية.

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
