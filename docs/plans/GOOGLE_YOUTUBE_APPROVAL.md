# موافقات Google/YouTube لـ Postiz

**اتكتب:** 2026-10-03 · **تبع:** [POSTIZ_MIGRATION_PLAN.md](POSTIZ_MIGRATION_PLAN.md) (المرحلة 1، جزء YouTube)

الـ OAuth app بتاع Postiz هو "Cortex Publisher"، على Google Cloud project اسمه `cortex-postiz`، والحساب gawadeditor@gmail.com. قدامه **مشكلتين منفصلتين**، وكل واحدة ليها حل مختلف:

| المشكلة | السبب | الحل | المدة |
|---|---|---|---|
| التوكن بيقع كل 7 أيام (`invalid_grant`) | الأبلكيشن **External + Testing** | ننقله لـ **In production** (المرحلة A) | 5 دقايق |
| الفيديوهات ممكن تتقفل **private** | أي project مش متعمله audit واتعمل بعد 28 يوليو 2020، YouTube بيقفل أي فيديو بيترفع منه بالـ API على private | **YouTube API Services Audit** (المرحلة C)، بس لو التجربة B أكدت إن ده بيحصلنا | من أسابيع لشهور |

الـ scopes اللي Postiz بيطلبها (`youtube.provider.ts`): `youtube`، و`youtube.force-ssl`، و`youtube.readonly`، و`youtube.upload`، و`youtubepartner`، و`yt-analytics.readonly`، و`userinfo.profile/email`. **كلها sensitive، ومفيهاش ولا واحدة restricted**، وده معناه إن مفيش CASA security assessment.

---

## المرحلة A: وقف وقوع التوكن ✅ (اتنقل لـ In production يوم 2026-10-03)

> اتعمل 2026-10-03: كمّلنا صفحة Branding (الصفحة الرئيسية، والخصوصية، والشروط على cortexchannels.com)، وبعدين **Publish app**، والحالة بقت **In production**. **✅ اتعملت إعادة الربط للقناتين (2026-10-03).** فيه bug في Postiz: لما بتعمل Add Channel لقناة موجودة أصلاً، الـ refresh token الجديد بيتحط في row مؤقت والـ row ده بيتمسح (`updateIntegration` بينقل `token` بس، ومش بينقل `refreshToken`). اتنقل الـ refresh token يدوي للـ rows الحقيقية بـ SQL، والمشغّل هو اللي شغّله بنفسه. Fingerprints: Hypogeum `aae2ef644f`، وTwinkle `ea495d025e`. واتأكدنا إن `GET /analytics/{id}` شغال للقناتين. **لو احتجنا نعمل إعادة ربط تاني، لازم ننقل الـ refresh token بنفس الطريقة.** في Google picker، الـ Hypogeum بتظهر باسم الـ brand account القديم "رادار | RADAR AI".

Google بيستثني الاستخدام الشخصي من الـ verification، يعني أي أبلكيشن مستخدمينه أقل من 100. التوكن بيقع كل 7 أيام **بس** طول ما الأبلكيشن في وضع Testing.

1. افتح Google Cloud Console، واختار project `cortex-postiz`، وبعدين **Google Auth Platform → Audience** (اسمها القديم OAuth consent screen).
2. **Publishing status: Testing**، اضغط **Publish app** ووافق. الحالة هتبقى "In production"، وممكن يكتب جنبها "needs verification"، وده عادي.
3. في Postiz (`https://postiz.cortexchannels.com`)، اعمل **Reconnect** لقناتين YouTube. هتظهرلك صفحة "Google hasn't verified this app"، اضغط **Advanced** وبعدين **Go to Cortex Publisher (unsafe)**. ده طبيعي لأنه أبلكيشننا إحنا.
4. **اتأكد بعد 8 أيام:** شوف إن YouTube لسه متوصل في Postiz (`GET /integrations`)، وإن مفيش `refreshNeeded`.

> ملحوظة: اختيار "Internal" مينفعش، لأنه محتاج Google Workspace، والقنوات على حساب gmail عادي.

## المرحلة B: هل الفيديوهات بتتقفل private؟ — الغالب لأ (2026-10-03)

> اتأكدنا من غير ما نرفع حاجة: فيديوهات تجربة Postiz اللي على Twinkle (`YyaVpNO_4nE` و`1oPTcBkMpM4`) مفيهاش أي إشعار "Locked" في Studio، واختيارات Unlisted/Public متاحة. فالغالب إن الـ project مش مقفول، والـ audit (المرحلة C) مش لازم. هنتأكد نهائياً مع أول بوست public حقيقي من Postiz.

تجربة المرحلة 0 كانت private من الأول، فمعرفناش منها حاجة. التجربة دي هتكشفها:

1. نرفع فيديو تجربة قصير على **Twinkle** من Postiz بـ `type: unlisted`.
2. نفتح الفيديو في YouTube Studio. **لو مكتوب "Locked (private)"** أو جاله إيميل بيقول "video locked as private"، يبقى الـ audit لازم (المرحلة C). **لو فضل unlisted عادي**، يبقى مش محتاجين المرحلة C.
3. بعدها نمسح الفيديو.

> الأدابتر جاهز للتجربة دي (`PostizProvider.publish(..., platform_options={"privacy": "unlisted", "made_for_kids": True})`)، بس محتاج binding وcredential لـ Postiz على Twinkle. **مش هعملها من غير موافقتك**، لأنها رفع حقيقي على القناة.

## المرحلة C: YouTube API Services Audit (لو B أكدت إننا محتاجينه)

الفورم: *YouTube API Services – Audit and Quota Extension Form* (من صفحة YouTube API Services في Google Developers). **ده مش الـ OAuth verification.**

إجابات جاهزة (بالإنجليزي زي ما الفورم بيطلب):

- **Organization / product:** Cortex Channels — internal publishing tool "Cortex Publisher" (self-hosted Postiz).
- **Website:** https://cortexchannels.com · **Privacy:** https://cortexchannels.com/privacy/ · **Terms:** https://cortexchannels.com/terms/
- **What the API client does:** Uploads original videos that Cortex Channels produces to the two YouTube channels it owns (The Hypogeum, Twinkle Mini Tales), sets their title, description, tags, privacy and made-for-kids status, and reads aggregate analytics for those same videos to improve future content.
- **Who uses it:** Only Cortex Channels staff, on channels we own. It is not offered to the public and it never accesses any other user's data.
- **API data stored:** OAuth tokens for our own channels, and aggregate statistics (views, watch time, likes, comments counts) for our own videos. No viewer personal data is collected.
- **Quota:** The default 10,000 units/day is enough (about 2–3 uploads/day plus analytics). No extension requested.
- **Made for kids:** Twinkle Mini Tales videos are published with `selfDeclaredMadeForKids=true`.

**فيديو الديمو (حوالي دقيقتين):** تصوير شاشة: (1) تسجيل الدخول لـ Postiz، (2) شاشة الـ OAuth consent والـ scopes، (3) إنشاء بوست YouTube بعنوان ووصف وprivacy وmade-for-kids، (4) الفيديو بعد ما يظهر في YouTube Studio، (5) صفحة التحليلات، (6) صفحة الخصوصية على cortexchannels.com.

## المرحلة D: OAuth Verification كاملة (اختيارية)

لازمة بس لو هنتعدّى 100 مستخدم، أو لو عايزين نشيل تحذير "unverified". الحاجات المطلوبة جاهزة من الأساس: الدومين متأكد، والصفحة الرئيسية وصفحة الخصوصية فيهم Limited Use. وناقص الآتي:
- تأكيد الدومين `cortexchannels.com` في **Google Search Console** بنفس الحساب.
- شرح لكل scope (ممكن نستخدم نفس الشرح اللي في المرحلة C) + فيديو الديمو نفسه.

---

## اللي اتعمل في الكود (2026-10-03)

- `hypogeum/publishing/postiz.py`: ده الـ `PostizProvider`. بيعمل publish لكل منصة لوحدها (multi-platform)، وبيستخدم نفس الـ idempotency بتاعة Zernio، وبيقرا الـ credential من Credential Manager بس، وكمان فيه `get_post` و`get_post_analytics` و`list_integrations`. اتسجل في `provider_registry`، **بس مش متوصّل بأي قناة**.
- اتأكدنا منه live (قراية بس): الـ 8 integrations وحالة بوستات المرحلة 0.
- **مستني:** الربط بالـ runtime (`zernio_publish_runtime`)، والـ bindings، والـ credential الحقيقي، والـ cover كأول frame عشان تيك توك، ومصدر التحليلات. كل ده بعد المرحلة A والتجربة B.
