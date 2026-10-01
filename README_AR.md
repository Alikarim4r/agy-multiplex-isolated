# AGY Multiplex Isolated

**مجدول تجريبي معزول بالحاويات لتشغيل Antigravity CLI (`agy`) عبر مجموعة ديناميكية من الحسابات والمشاريع.**

العربية · [English](README.md)

> الحالة: **0.1.0-alpha**. التشغيل الحي المتعدد مقفول افتراضيًا حتى اجتياز اختبارات المختبر.

## الفكرة

لا يوجد في البرنامج عدد ثابت للحسابات أو للمشاريع. يمكنك إضافة أي عدد من الحسابات وأي عدد من المشاريع، بينما تحدد موارد الجهاز وDocker وحصة المزود القدرة الفعلية على التنفيذ.

السعة المنطقية تساوي:

```text
عدد الحسابات المختارة × عدد الـ slots لكل حساب
```

ويمكن استخدام `max_workers` لوضع سقف عالمي أقل.

لكل حساب Docker HOME رئيسي مستقل، ولكل lane نسخة مؤقتة مستقلة من ذلك الـHOME مع container وGit worktree منفصلين.

## استخدام 5 ساعات والأسبوع

تعرض واجهة تسجيل الحسابات المحلية ثنائية اللغة **نسبة الاستخدام** لنافذة 5 ساعات المتحركة وللنافذة الأسبوعية. ولأن Antigravity يفصل عادةً حصة **Gemini** عن حصة **Claude / GPT**، تعرض الواجهة المجموعتين عندما تكونان متاحتين.

تقرأ الأداة بيانات الحصة من أمر CLI الرسمي للحساب المسجل: `agy -p /usage --output-format json`. في الإصدار المثبت `agy 1.2.14` يعيد الأمر غلاف JSON دقيقًا، بينما يحتوي الحقل `response` على صفوف مفصولة بعلامات Tab تشمل مجموعة النماذج، نافذة الحصة، **نسبة المتبقي** ووقت إعادة الضبط. تحسب الواجهة `الاستخدام = 100 - المتبقي`. التحديث التلقائي للحصة مغلق افتراضيًا ويمكن التحديث يدويًا؛ ويؤجل التحديث أثناء تسجيل الدخول أو وجود lane نشطة للحساب نفسه، كما تُربط البيانات المحفوظة بجيل بيانات اعتماد الحساب الموثّق حتى لا تظهر حصة قديمة بعد تغيير الهوية، ولا تُرسل بيانات الاعتماد الخام إلى المتصفح.

تمت ملاحظة عقد `/usage` فعليًا على جلسة محلية موثقة باستخدام `agy 1.2.14`، وكان `num_turns = 0` وجميع عدادات الـtokens تساوي صفرًا. يحتفظ الاختبار بعينة منقّحة لنفس شكل الاستجابة. هذا يثبت parsing وعرض الحصة، لكنه **لا** يثبت أمان تحديث OAuth المتزامن أثناء multiplex؛ وتبقى تلك المسألة ضمن [LAB_GATE](LAB_GATE.md). وإذا تعذر التحقق من الحصة تعرض الواجهة «غير متاح» بدل اختراع نسبة.

## نموذج الأمان

- volume رئيسي مستقل لكل حساب.
- volume مؤقت مستقل لكل lane أثناء التنفيذ.
- لا يتم تبديل الحسابات عبر Antigravity Manager أثناء تشغيل الـlanes.
- لا يتم تركيب HOME الخاص بالمضيف أو Apple Keychain داخل حاويات التنفيذ.
- إذا لم تطابق هوية التنفيذ الحساب المرتبط يتم رفض الـlane.
- كل مهمة مقيدة بـ`write_scope` محدد.
- الأداة لا تقوم بـpush أو merge إلى main أو deploy.
- التشغيل الحي مقفول افتراضيًا.
- ملفات الحالة المحلية خارج المستودع في `~/.agy-multiplex-isolated` افتراضيًا.

راجع [SECURITY.md](SECURITY.md) و[LAB_GATE.md](LAB_GATE.md).

## المتطلبات

- Python 3.11 أو أحدث.
- Docker Desktop أو Docker Engine.
- Git.
- اتصال بالإنترنت لتثبيت واستخدام Antigravity CLI الرسمي.

الدعم في هذه النسخة التجريبية موجّه إلى macOS وLinux. WSL2 قد يعمل عبر مسار Linux لكنه ليس هدفًا مختبرًا بعد.

## التثبيت

```bash
git clone <your-repository-url>
cd AGY-MULTIPLEX-ISOLATED
./install.sh
agy-multiplex-isolated build-image
agy-multiplex-isolated init
```

## إضافة الحسابات

يمكن إضافة أي عدد من الحسابات بدون تعديل الكود:

```bash
agy-multiplex-isolated add-account personal
agy-multiplex-isolated add-account work-1
agy-multiplex-isolated add-account work-2
agy-multiplex-isolated accounts
```

ثم تسجيل وربط كل حساب:

```bash
agy-multiplex-isolated login work-1
agy-multiplex-isolated bind work-1
```

أو استخدم واجهة تسجيل الدخول العربية/الإنجليزية:

```bash
agy-account-login-ui
```

الواجهة تعمل على `127.0.0.1` فقط ولا تعرض OAuth tokens.

## إضافة المشاريع

يمكن تسجيل أي عدد من مستودعات Git:

```bash
agy-multiplex-isolated add-project \
  --name saferim \
  --repo ~/saferim \
  --plan ~/plans/saferim.json \
  --goal "Implement the approved scope"

agy-multiplex-isolated projects
```

يمكن تعطيل أو تفعيل الحسابات والمشاريع بدون حذف بياناتها:

```bash
agy-multiplex-isolated disable-account work-2
agy-multiplex-isolated enable-account work-2
agy-multiplex-isolated disable-project saferim
agy-multiplex-isolated enable-project saferim
```

`remove-account` يحذف التسجيل من الأداة فقط ويحافظ على Docker credential volume. و`remove-project` لا يحذف المستودع.

## التشغيل

بدون `--manifest` تستخدم الأداة كل الحسابات والمشاريع المفعّلة في السجل:

```bash
agy-multiplex-isolated validate
agy-multiplex-isolated dry-run
agy-multiplex-isolated run --lab --max-workers 4
```

الـmanifest اختياري، ويمكن استخدامه لاختيار مجموعة محددة أو تغيير التوازي:

```json
{
  "accounts": "all",
  "projects": "all",
  "per_account_slots": 3,
  "max_workers": 12
}
```

لا يوجد حد برمجي ثابت لعدد سجلات الحسابات أو المشاريع، كما أن `per_account_slots` قابل للضبط. استخدم أرقامًا تناسب موارد جهازك وحصة المزود.

## بوابة التشغيل الحي

بعد اجتياز اختبارات المختبر الموثقة فقط:

```bash
agy-multiplex-isolated enable-live --ack "I ACCEPT LAB GATE REQUIREMENTS"
```

ويمكن إغلاق التشغيل الحي مجددًا:

```bash
agy-multiplex-isolated disable-live
```

## التطوير والفحص

```bash
make check
make test
```

ملفات الحالة والاعتماد لا تُخزن داخل المستودع. مسار البيانات الافتراضي هو `~/.agy-multiplex-isolated` ويمكن تغييره عبر `AGY_MULTIPLEX_HOME`.

## الترخيص

MIT. راجع [LICENSE](LICENSE).
