# 完整首版示例：竖版9:16四宫格

两个示例都输出一张竖版9:16、2×2的四宫格：左上 FACE FRONT、右上 FACE SIDE、左下 BODY FRONT、右下 BACK。使用时替换角色身份、五官和衣装，保持自然皮肤细节与材质作用范围；不继承示例的性别、职业或配件。示例B不描述人物，人物细节以用户上传的参考图为准，只需按需改背景、光色和遮脸状态。

## 示例A：无参考图，新建角色（年轻行医者）

```text
Create one portrait 9:16 live-action costume-fitting photograph arranged as a 2x2 grid of four equal panels with thin light gutters and one small white sans-serif corner label per panel: top-left "FACE FRONT", top-right "FACE SIDE", bottom-left "BODY FRONT", bottom-right "BACK". The same fictional adult actress appears in all four panels on one seamless neutral mid-grey studio background.

PANEL 1, FACE FRONT: head-and-shoulders portrait, face straight toward the camera at eye level, calm neutral expression, eyes engaged but relaxed. PANEL 2, FACE SIDE: same scale, clean 90-degree profile facing frame-left, same expression and hair behaviour. PANEL 3, BODY FRONT: full-body standing pose, front view, arms relaxed at her sides, empty hands, parallel feet; the face is intentionally featureless and low-detail, a smooth matte skin-toned blank oval with no eyes, brows, nose or mouth and a soft edge, reserved for later editing; hair, hairline, neck and collar stay exactly as in the other panels. PANEL 4, BACK: full-body rear view, hair crown to shoes visible, same standing spot, same height and same shoe baseline as panel 3, face turned away. Full-body figures fill roughly 90% of the panel height, with both hands, both shoes and all garment edges inside the frame.

ACTRESS: a 29-year-old Chinese travelling physician in a fictional late-imperial setting, slim ordinary adult build. Slightly broad oval face, softly squared jaw, modest straight nose, dark brown almond-shaped eyes, natural eyebrows with slight asymmetry, closed relaxed lips. Black hair in a compact low bun with one plain dark wooden hairpin and a few natural loose wisps. Keep identity, age, hair and body consistent across all four panels.

SKIN (panels 1 and 2 and visible hands): natural human skin with warm medium-light tones, age- and distance-appropriate pores, fine facial hair, subtle eye-area detail and slight local colour variation; matte finish with only restrained natural highlights, no gloss. Preserve facial structure, expression and natural asymmetry, without beauty retouching.

WARDROBE: plain muted sage-grey cotton cross-collar long tunic, right-over-left overlap, wrist-length sleeves, narrow dark brown cloth belt tied at her anatomical left hip. One slim rectangular dark brown leather medicine case is fixed at her anatomical right hip: viewer-left in front, viewer-right in back. Dark umber straight trousers and simple dark brown flat cloth shoes. Lock garment construction, accessory count and anatomical placement across panels. Cotton has believable thickness, visible woven structure at the appropriate scale, weight-bearing folds and slight seam tension. Minor wear belongs only to cuff edges and shoe soles. The leather case has restrained highlights. Material wear stays on garments and accessories.

STUDIO SETUP (identical in all four panels): seamless neutral mid-grey background, a single soft-edged key light at 45 degrees camera-left, neutral light colour, no fill light, no rim light, no set dressing, no props. Only natural contact shadows on the floor. Neutral white balance, faithful skin colour, readable cloth structure, natural low-distortion perspective for full-body panels and an eye-level portrait perspective around 85mm for panels 1 and 2.

AVOID: text other than the four corner labels, extra panels or views, identity drift between panels, mirrored accessories, a changed pose or height between panels 3 and 4, CG appearance, beauty-filter smoothing, ambient colour spill.
```

## 示例B：上传了人物参考图，转成四宫格

用户上传的成年人物参考图是唯一设计来源。把参考图通过工具的图像引用机制传入，提示词里写保留约束与逐格取景，不再重新描述人物。下面的结构和大部分措辞沿用用户提供的教师案例（IMAGE TASK、CRITICAL PRESERVATION OVERRIDE、OUTPUT FORMAT、PANEL 1–4、STUDIO SETUP、QUALITY、AVOID）。教师案例的 AVOID 中，FACE 与 BODY 两条的部分文字被截图上的字幕遮住，BODY 条中间的 “changed or added accessories” 是按可见部分补全的，不是原文。

```text
IMAGE TASK: Convert the uploaded character reference image(s) into a 4-panel character sheet.

CRITICAL PRESERVATION OVERRIDE: The uploaded image(s) are the ONLY design source. Preserve EXACTLY: facial structure, eyes, nose, mouth shape, hairline and hairstyle, age, skin tone and skin texture (real pores, any natural stubble or facial hair), body proportion and weight, every garment detail (cut, fabric, seams, wear, hardware), footwear. NO beautification, NO redesign, NO style drift, NO "improving" the face. The same person, four times: one identity across all panels. Where the reference does not show a view, extend it conservatively; add no new patterns or accessories.

OUTPUT FORMAT: single portrait 9:16 image, 2x2 grid, four equal panels, thin light gutters, small white sans-serif panel labels in the top-left corner of each panel: "FACE FRONT", "FACE SIDE", "BODY FRONT", "BACK".

PANEL 1, TOP LEFT, FACE FRONT: head-and-shoulders close-up, face directly toward camera at eye level, calm neutral expression, eyes engaged but relaxed.
PANEL 2, TOP RIGHT, FACE SIDE: same head-and-shoulders scale, clean 90-degree profile, same expression, same hair behaviour.
PANEL 3, BOTTOM LEFT, BODY FRONT: full-body standing pose, front view, arms relaxed at sides, weight natural. The face is intentionally featureless and low-detail, a smooth matte skin-toned blank oval with no eyes, brows, nose or mouth and a soft edge (reserved for later editing). Hair, hairline, neck and collar stay exactly as in panel 1.
PANEL 4, BOTTOM RIGHT, BACK: full-body standing pose, rear view, hair from behind, garment back seams, heel height, same standing spot, same height and same shoe baseline as panel 3. The full-body figures fill about 90% of the panel height and every outer garment edge stays inside the frame.

STUDIO SETUP (all four panels identical): seamless neutral mid-grey studio background, a single soft-edged key light at 45 degrees camera-left, neutral light colour, NO fill light, no rim light, no set dressing, no props. Only natural contact shadows on the floor.

QUALITY: photographic character sheet for film production, even exposure, accurate fabric weave, matte skin, real pores, no gloss. Clean, clinical, production-use.

AVOID:
- FACE (panels 1 and 2): no beauty-filter skin, no glossy TV-drama complexion, no idol makeup, no plastic skin, no face slimming, no enlarged eyes, no skin smoothing.
- BODY: no pose change between panels, no changed or added accessories, no height change.
- LIGHT: no ambient colour spill, no rim-light drama.
- TEXT: no text other than the four labels.
```

与教师案例原文相比，示例B有以下改动，需要时可以改回：

- 版式：加了竖版9:16、角标位置（各格左上角）、角标用白色小号无衬线字。
- 背景与光：原文的背景颜色与光色是两个占位符（BG、LIGHT），已填成“中灰”和“中性”，需要别的颜色时替换这两处。原词是 hard key light，示例B写成 soft-edged，因为示例图的地面阴影偏柔；想要硬光时直接写 hard，并接受深色衣装在无补光下暗部细节更容易丢失。STUDIO SETUP 里另加了 no rim light，AVOID 里保留原有的 no rim-light drama。
- PANEL 1、2：PANEL 1 加了 at eye level，PANEL 2 的 clean profile view 改成 clean 90-degree profile，避免模型给出四分之三侧。
- PANEL 3：把“脸有意无五官、低细节”展开成“平滑哑光肤色椭圆，无眼眉鼻嘴，边缘柔和”，并写明头发、发际线、颈部、衣领保持不变。
- PANEL 4：加了“same height and same shoe baseline as panel 3”，并要求全身人物约占格高90%、外层衣装边缘不出框。
- 皮肤：原文的 “natural stubble” 改成 “any natural stubble or facial hair”，避免给没有胡须的角色加胡茬。
- 其他：加了“参考图没有显示的视角保守延展，不新增图案或配件”一句，以及 AVOID 里的 TEXT 一条。

## BODY FRONT 的两种写法

只替换 PANEL 3 里关于脸部的句子，其他格不动。

**方案A：空白脸（默认）**

```text
PANEL 3, BOTTOM LEFT, BODY FRONT: full-body standing pose, front view, arms relaxed at sides, weight natural. The face is intentionally featureless and low-detail: a smooth matte skin-toned blank oval with no eyes, brows, nose or mouth and a soft edge, reserved for later editing. Hair, hairline, neck and collar stay exactly as in panel 1.
```

**方案B：无头（做不到空白脸时）**

```text
PANEL 3, BOTTOM LEFT, BODY FRONT: full-body costume view of the same living person, cropped by the camera from the collarbones down to the soles; her head is outside the upper edge of this panel. Both hands and both shoes are fully visible, arms relaxed at sides, weight natural. Shoe soles rest on the same floor baseline as panel 4. This is a photographic crop of a living person, with no severed neck, flat cut surface, mannequin or wound.
```

选方案B时，检查 PANEL 4 的“same height”：BODY FRONT 因锁骨在上缘，人物会比 BACK 放大约两到三成（估算，见 SKILL.md），把“same height”改为“same standing spot and same shoe baseline”，不要求两格同高。

## 按角色调整

- 科幻装备：替换衣装段，保留用户要求的层次、连接点、承重、数量、位置和开关状态；不用额外的皮肤纹理强化科幻感。
- 面罩与兜帽：逐格指定穿戴状态。FACE FRONT 与 FACE SIDE 露脸不改变 BODY FRONT 与 BACK 的面罩状态；参考图中 BODY FRONT 佩戴面罩时照此保留，不再叠加空白脸；采用无头方案时面罩在框外，不挪到胸前或腰侧展示。摘下的面罩无需出镜。
- 老年、雀斑与伤疤：在身份或肤质段保留用户明确要求的特征，正常老年纹路不作为起皮伪影清除；特效妆只作用于指定区域。
- 深色衣装：同色系深色衣料（如全黑的蕾丝、缎面、棉麻）靠材质区分层次：写明哪层是哑光、哪层有受控的缎面高光、哪层是半透明蕾丝，不靠提亮整体曝光；背景用中灰，让黑色轮廓可读。
- 只参考排版：首段说明参考图仅提供布局，演员与服装依用户新设定；不把参考人物的肤质、年龄、道具或磨损一并继承。

## 已认可原图的局部修复

仅用户授权修图时使用以下短编辑指令，按实际问题替换位置。通过工具参数引用原图；不要附加整份新角色提示词。

**修 BODY FRONT 的脸（模糊不干净、或仍带五官）**

```text
Edit the supplied reference image. Keep the 2x2 four-panel layout, all corner labels, every panel's framing, the actor identity in panels 1 and 2, hair, costume, equipment, lighting and floor shadows exactly as shown. Only in the bottom-left BODY FRONT panel, replace the face area with a smooth matte skin-toned blank oval with no eyes, brows, nose or mouth and a soft edge that blends into the hairline and neck. Leave hair, neck, collar and the rest of the body untouched; no blur outside the face area.
```

**把 BODY FRONT 改为无头**

```text
Edit the supplied reference image. Keep the top two panels and the bottom-right BACK panel exactly as shown. In the bottom-left BODY FRONT panel only, crop the framing to start at the collarbones so the head is outside the upper edge; both hands, the costume and both shoes stay fully visible and the shoe baseline stays the same. A photographic crop of a living person: no severed neck, flat cut surface or mannequin look.
```

**清除局部异常（如鼻翼白屑）**

```text
Edit the supplied reference image. Preserve its actor identity, facial anatomy, expression, hair, lighting, skin colour variation, natural pores and fine facial hair, costume, equipment, framing and four-panel layout. Only remove the small anomalous white flakes beside the nose in the top-left FACE FRONT panel, restoring the surrounding natural skin surface. Preserve all other regions and normal age-appropriate skin detail; no overall smoothing or redesign.
```
