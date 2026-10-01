# خطة النقل من Zernio لـ Postiz

**بدأت:** 2026-10-01 · **الحالة:** المرحلة 0 (تجربة محلية) شغالة · **Zernio:** بنكمل عليه لحد ما النقل يخلص

## ليه

- Zernio بيكلف من **$29 لـ $36 في الشهر** على 8 حسابات (قناتين × 4 منصات)، وده هيزيد مع كل قناة جديدة (حوالي $24 في الشهر لكل قناة).
- **Postiz** ([gitroomhq/postiz-app](https://github.com/gitroomhq/postiz-app)) مفتوح المصدر (AGPL-3.0)، وعليه 36.6k نجمة، وشغال عليه لحد دلوقتي. بيدعم YouTube وTikTok وInstagram وFacebook، وفيه **API** للمصنع (`POST /public/v1/posts` و`/upload` و`/integrations`).
- التكلفة بعد النقل: **سيرفر بس (حوالي $6 لـ $12 في الشهر) + دومين (حوالي $10 في السنة)**. والسيرفر نفسه بيحل مشكلة نوم الماك.

## اللي Postiz مش هيحله

- **cover الـ YouTube Shorts:** نفس المشكلة اللي عندنا مع Zernio، لأنها حد من YouTube نفسه.
- **cover تيك توك:** الـ API بتاع Postiz لـ TikTok مفيهوش حقل للـ cover. **الحل:** نحط الـ cover كأول frame في الفيديو جوه الـ assembly بتاعنا، وده نفس اللي Zernio بيعمله ([مواصفات Zernio](https://zernio.com/openapi.json): *"stitched as a single frame at the start of the video"*).
- **التحليلات:** المصنع واخد كل المشاهدات واللايكات من Zernio Analytics. لازم نتأكد في المرحلة 0 إن Postiz بيطلّع نفس البيانات، ولو لأ، نسحبها من APIs المنصات نفسها.

---

## المرحلة 0: تجربة محلية على الماك (دلوقتي، ببلاش)

**الهدف:** نتأكد إن Postiz بيعمل كل اللي المصنع محتاجه، قبل ما نصرف وقت على موافقات المنصات.

- [x] تشغيل Postiz بـ Docker في `~/postiz-trial`، على `http://localhost:4007`، ومفتوح على الماك بس.
  - Docker على الماك ده لازم يتشغل بـ: `env -u DOCKER_HOST docker --context desktop-linux compose ...`
    (المتغير `DOCKER_HOST` اللي على الجهاز بيوصّل لـ socket مبيشتغلش، وكل `pull` بيرجع "Not Found").
- [x] حساب الأدمن اتعمل (2026-10-01).
- [ ] بعدها نقفل التسجيل (`DISABLE_REGISTRATION: 'true'`).
- [x] **YouTube بتاع Twinkle اتربط** (2026-10-01). Google Cloud project `cortex-postiz` على **gawadeditor@gmail.com** (الحساب اللي القنوات عليه)، والـ YouTube Data وAnalytics وReporting APIs متفعلين، والـ OAuth app اسمه "Cortex Publisher" (External، Testing)، وgawadeditor test user، والـ Web client redirect هو `http://localhost:4007/integrations/social/youtube`. الـ credentials في `~/postiz-trial/docker-compose.yaml` (chmod 600).
- [ ] نربط **Facebook وInstagram بتوع Twinkle** في وضع التطوير (Meta Development mode، وحسابك ليه role في الأبلكيشن).
- [ ] تيك توك محتاج HTTPS حتى على الجهاز المحلي، فهيتأجل لحد ما يبقى عندنا دومين، أو نعمل شهادة محلية.
- [ ] **اختبارات الـ API** (على فيديو private):
  - [ ] رفع ميديا (`/upload`) ونشر مجدول (`type: schedule`, `date`).
  - [ ] YouTube: عنوان، و`type` (public/private/unlisted)، و`selfDeclaredMadeForKids`، و`tags`.
  - [ ] ميعاد مختلف لكل منصة، يعني post منفصل لكل integration.
  - [ ] حالة البوست بعد النشر، والـ webhooks.
  - [x] الـ API شغال على `http://localhost:4007/api/public/v1` (الـ header: `Authorization: <key>`، والمفتاح في `~/postiz-trial/.api_key`). `GET /integrations` شغال.
  - [~] **التحليلات:** `GET /analytics/{integration-id}?date=30` شغال لـ YouTube (minutes watched، وavg view duration و%، وsubs gained/lost، وlikes)، **بس مفيهوش عدد المشاهدات**. الـ per-post (`GET /analytics/post/{postId}`) بيشتغل بس على البوستات اللي اتنشرت من Postiz نفسه. **لسه:** نتأكد إن فيه views per post.
  - ملحوظة: حد الـ API الافتراضي **30 طلب في الساعة** (`API_LIMIT` في الـ compose، وينفع نزوده).
- [ ] **نقطة قرار:** لو كل ده اشتغل، نكمل للمرحلة 2. ولو التحليلات ناقصة، نحدد بديلها الأول.

## المرحلة 1: موافقات المنصات (نبدأها دلوقتي بالتوازي، وبتاخد من 2 لـ 6 أسابيع)

**مطلوب قبل أي موافقة:**
- [ ] **دومين** (حوالي $10 في السنة)، لو مش موجود.
- [ ] **موقع HTTPS** على الدومين فيه **Privacy Policy** و**Terms of Service**. ينفع مجاناً على GitHub Pages، وأنا أكتب الصفحتين.
- [ ] بيانات الشركة أو الشخص اللي هيتسجل كـ developer (Meta بتطلب Business Verification بمستندات رسمية).

### YouTube (Google Cloud)
- [ ] Google Cloud project، وتفعيل **YouTube Data API v3** و**YouTube Analytics API** و**YouTube Reporting API**.
- [ ] OAuth consent screen: **External**، لأن القنوات Brand accounts، وإيميلك test user في الأول.
- [ ] OAuth client من نوع **Web application**، والـ redirect:
  - للتجربة: `http://localhost:4007/integrations/social/youtube`
  - للإنتاج: `https://<domain>/integrations/social/youtube`
- [ ] **التقديم على Verification** (بعد ما الدومين والصفحات يجهزوا): من غيرها التوكن بيقع كل 7 أيام. وده سبب "invalid_grant" اللي بيحصل عندنا من 23 سبتمبر.
- ملحوظة الـ quota: الافتراضي 10,000 unit في اليوم، والرفع الواحد بحوالي 1,600، يعني حوالي 6 رفعات في اليوم، وده كفاية.

### Instagram + Facebook (Meta)
- [ ] Meta developer account، وأبلكيشن من نوع **Business**.
- [ ] نضيف **Instagram** (Instagram Business Login) و**Facebook Login for Business**.
- [ ] الـ redirect: `/integrations/social/instagram` و`/integrations/social/facebook`.
- [ ] **Business Verification**.
- [ ] **App Review** للصلاحيات دي: `instagram_basic` و`pages_show_list` و`pages_read_engagement` و`business_management` و`instagram_content_publish` و`instagram_manage_comments` و`instagram_manage_insights` (وصلاحيات نشر الـ Pages لفيسبوك). وبيطلبوا فيديو بيشرح الاستخدام.
- المدة المتوقعة: **أكتر من شهر**.

### TikTok
- [ ] TikTok developer account، وأبلكيشن فيه **Login Kit** و**Content Posting API (Direct Post)**.
- [ ] Scopes: `user.info.basic` و`user.info.profile` و`video.create` و`video.upload` و`video.publish`.
- [ ] Redirect: `https://<domain>/integrations/social/tiktok` (لازم HTTPS)، وتأكيد ملكية الدومين.
- [ ] App review، وبعده **audit منفصل لـ Direct Post**. **لحد ما الـ audit يخلص، كل البوستات هتنزل private.**
- ⚠️ **أكبر خطر في الخطة كلها:** تيك توك بيراجع إن الأبلكيشن بيلتزم بتعليماته في الواجهة (اختيار الـ privacy، والموافقة على المحتوى). ممكن يرفض أداة داخلية. ولو رفض، نفضل على Zernio لتيك توك بس، وده حوالي $12 في الشهر.

## المرحلة 2: السيرفر (بعد الموافقات)

- [ ] VPS: 2 vCPU و2 لـ 4 GB RAM، و Ubuntu 24.04 (حوالي $6 لـ $12 في الشهر).
- [ ] Docker، وPostiz، وCaddy عشان الـ HTTPS، وbackup يومي لقاعدة البيانات.
- [ ] (اختياري، ومحتاجه جداً) ننقل المصنع كله على نفس السيرفر عشان يشتغل 24 ساعة. ده محتاج أولاً push الكود لـ GitHub، و**قبله لازم نغيّر مفتاح OpenRouter** لأنه موجود في تاريخ git.

## المرحلة 3: ربط المصنع

- [ ] `hypogeum/publishing/postiz.py`: provider جديد بنفس واجهة `ZernioProvider` (النشر، والحالة، وتحديث الـ privacy).
- [ ] نختار المزود لكل قناة من `channel_provider_bindings`، عشان ننقل قناة قناة.
- [ ] نحط الـ cover كأول frame في الـ assembly، عشان تيك توك.
- [ ] نبدّل مصدر التحليلات، حسب نتيجة المرحلة 0.
- [ ] الـ YouTube cover hold (`youtube_cover_hold.py`) يشتغل مع Postiz كمان (`type: private`)، لو قررنا نكمل بيه.

## المرحلة 4: التبديل

- [ ] **Twinkle الأول** (مخاطرتها أقل): أسبوعين في نفس الوقت مع Zernio، وبعدها نشيل Twinkle من Zernio.
- [ ] **Hypogeum بعدها.**
- [ ] نلغي Zernio، أو نسيبه لتيك توك بس لو الـ audit اترفض.

## Zernio خلال المدة دي

- لازم الكارت يتشحن ($18 مطلوبين، والحساب موقوف من 2026-10-01). من غيره **مفيش أي نشر**.
- لتقليل الفاتورة لحد النقل: ممكن نفصل YouTube وInstagram بتوع Twinkle، لأنهم الأضعف أداءً، فنوفر حوالي $12 في الشهر.
