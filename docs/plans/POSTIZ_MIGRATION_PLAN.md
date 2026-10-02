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
- [x] التسجيل اتقفل (`DISABLE_REGISTRATION: 'true'`، 2026-10-01).
- [x] **Postiz بقى على `https://postiz.cortexchannels.com`** (2026-10-01) عن طريق Cloudflare Tunnel اسمه `cortex-postiz` (`~/.cloudflared/config.yml`، الـ hostname ده بس → `127.0.0.1:4007`). الحماية (SSRF) رجعت شغالة، والـ forwarder مبقاش لازم. الـ script: `~/postiz-trial/go-public.sh` (والرجوع: `docker-compose.yaml.bak-localhost`). ⚠️ التانل شغال بـ nohup، **بيقع مع أي restart** لحد ما نخليه service على السيرفر.
- [x] **YouTube بتاع Twinkle اتربط** (2026-10-01). Google Cloud project `cortex-postiz` على **gawadeditor@gmail.com** (الحساب اللي القنوات عليه)، والـ YouTube Data وAnalytics وReporting APIs متفعلين، والـ OAuth app اسمه "Cortex Publisher" (External، Testing)، وgawadeditor test user، والـ Web client redirect هو `http://localhost:4007/integrations/social/youtube`. الـ credentials في `~/postiz-trial/docker-compose.yaml` (chmod 600).
- [x] **YouTube بتاع Hypogeum اتربط** (2026-10-01).
- [x] **Facebook Pages الاتنين اتربطوا** (The Hypogeum وtwinkleminitales)، و**Instagram بتاع Hypogeum** (2026-10-01)، بأبلكيشن Meta "Cortex Publisher" (`1406469254364317`، Business، Development mode، Facebook Login for Business).
- [x] **Instagram بتاع Twinkle اتربط** (2026-10-01): ربطناه بصفحة twinkleminitales من إعدادات الصفحة (Linked accounts → Instagram)، وبعدين في Postiz. **كل الحسابات الـ 8 متربطة.**
- [~] **TikTok (sandbox):** **Twinkle اتربط** (2026-10-01). Direct Post حتى بـ "Only me" بيترفض (`unaudited_client_can_only_post_to_private_accounts`): لحد الـ audit **الحساب كله** لازم يبقى private، ومعملناش كده. وضع **Upload** (draft في inbox التطبيق) اشتغل واتأكدنا منه. **Hypogeum اتربط كمان** (thehypogeum1).
- [ ] **اختبارات الـ API** (على فيديو private):
  - [ ] رفع ميديا (`/upload`) ونشر مجدول (`type: schedule`, `date`).
  - [ ] YouTube: عنوان، و`type` (public/private/unlisted)، و`selfDeclaredMadeForKids`، و`tags`.
  - [ ] ميعاد مختلف لكل منصة، يعني post منفصل لكل integration.
  - [ ] حالة البوست بعد النشر، والـ webhooks.
  - [x] الـ API شغال على `http://localhost:4007/api/public/v1` (الـ header: `Authorization: <key>`، والمفتاح في `~/postiz-trial/.api_key`). `GET /integrations` شغال.
  - [~] **التحليلات:** `GET /analytics/{integration-id}?date=30` شغال لـ YouTube (minutes watched، وavg view duration و%، وsubs gained/lost، وlikes)، **بس مفيهوش عدد المشاهدات**. الـ per-post (`GET /analytics/post/{postId}`) بيشتغل بس على البوستات اللي اتنشرت من Postiz نفسه. ✅ **اتأكدنا (2026-10-01):** `GET /analytics/post/{postId}` بيرجّع **Views** وLikes وComments وFavorites للبوست اللي اتنشر من Postiz.
  - ملحوظة: حد الـ API الافتراضي **30 طلب في الساعة** (`API_LIMIT` في الـ compose، وينفع نزوده).
- [x] **اختبار نشر حقيقي على YouTube (2026-10-01):** رفع (`/upload`، 40MB، 1080×1920) ثم بوست `schedule` **private** على Twinkle، واتنشر خلال حوالي دقيقة من الميعاد (`YyaVpNO_4nE`، private اتأكدنا منه). المحاولة الأولى فشلت بـ "Blocked IP": Postiz بيجيب الميديا من رابطه هو (`http://localhost:4007/uploads/...`)، والحماية بتاعته (SSRF) بتمنع الوصول للعناوين المحلية، وكمان `localhost:4007` مش موجود جوه الـ container (بيسمع على 5000). **حل التجربة بس:** `DISABLE_SSRF_PROTECTION: 'true'` في الـ compose، وforwarder من 4007 لـ 5000 جوه الـ container (`docker exec -d postiz node -e ...`، **بيتشال مع أي restart**). في الإنتاج بدومين عام المشكلتين مش هيظهروا.
- [ ] **نقطة قرار:** لو كل ده اشتغل، نكمل للمرحلة 2. ولو التحليلات ناقصة، نحدد بديلها الأول.

## المرحلة 1: موافقات المنصات (نبدأها دلوقتي بالتوازي، وبتاخد من 2 لـ 6 أسابيع)

**مطلوب قبل أي موافقة:**
- [x] **الدومين: `cortexchannels.com`** (Cloudflare Registrar، اتسجل 2026-10-01، بيخلص 2027-10-01، والـ nameservers على Cloudflare).
- [x] **الموقع شغال:** https://cortexchannels.com/ ، و`/privacy/` و`/terms/` و`/data-deletion/`. منشور كـ Cloudflare Worker static assets (`site/`، والنشر بـ `cd site && CLOUDFLARE_ACCOUNT_ID=08de2c20cf5b5c85ea8a34d910fa2bda npx wrangler deploy`). وإيميل **privacy@cortexchannels.com** بيتحول لـ gawadeditor@gmail.com (Cloudflare Email Routing).
- [ ] بيانات الشركة أو الشخص اللي هيتسجل كـ developer (Meta بتطلب Business Verification بمستندات رسمية).

### YouTube (Google Cloud)
- [x] الـ redirect بتاع الدومين العام اتضاف (2026-10-01).
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
- [x] الـ redirect: `/integrations/social/instagram` و`/integrations/social/facebook` على `postiz.cortexchannels.com` (2026-10-01).
- [ ] **Business Verification**.
- [ ] **App Review** للصلاحيات دي: `instagram_basic` و`pages_show_list` و`pages_read_engagement` و`business_management` و`instagram_content_publish` و`instagram_manage_comments` و`instagram_manage_insights` (وصلاحيات نشر الـ Pages لفيسبوك). وبيطلبوا فيديو بيشرح الاستخدام.
- المدة المتوقعة: **أكتر من شهر**.

### TikTok
- [x] الأبلكيشن `7653339412022134791` (كان draft اسمه Hypogeum، بقى **Cortex Publisher**)، فيه **Login Kit** و**Content Posting API** و**Direct Post** شغال.
- [x] الـ 6 scopes اللي Postiz بيطلبهم (ولو واحد ناقص بيرفض الربط): `user.info.basic` و`user.info.profile` و`user.info.stats` و`video.list` و`video.upload` و`video.publish`.
- [x] Redirect: `https://postiz.cortexchannels.com/integrations/social/tiktok`، و**الدومين cortexchannels.com اتأكد** (DNS TXT).
- [x] شرح الـ review مكتوب، وفيديو الديمو اتصور (`~/Desktop/cortex_publisher_tiktok_demo.mp4`).
- [x] **اتبعت للمراجعة 2026-10-02** (In review). ملحوظة: نسخة الـ Production ماكانتش حافظة أي تعديل قبل كده، فاتعملت من الأول واتأكدنا منها بإعادة التحميل.
- [x] الـ Sandbox "Hypogeum-Dev" اتصلّح: كان بيشاور على hypogeum.com (مش بتاعنا) وredirect على webhook.site عام.
- [ ] App review، وبعده **audit منفصل لـ Direct Post**. **لحد ما الـ audit يخلص، كل البوستات هتنزل private.**
- ⚠️ **أكبر خطر في الخطة كلها:** تيك توك بيراجع إن الأبلكيشن بيلتزم بتعليماته في الواجهة (اختيار الـ privacy، والموافقة على المحتوى). ممكن يرفض أداة داخلية. ولو رفض، نفضل على Zernio لتيك توك بس، وده حوالي $12 في الشهر.

## المرحلة 2: السيرفر (بعد الموافقات)

التفاصيل في [SERVER_PLAN.md](SERVER_PLAN.md): Hostinger KVM 2 (8GB، $8.99 في الشهر بعرض الـ 24 شهر، والدفع بـ PayPal) لـ Postiz بس، والمصنع يفضل على الماك. الـ Mac mini القديم (4GB) مش هينفع.

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
