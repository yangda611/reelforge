# 迁移示例

示例均为新写的提示词，用于说明生成方法，不是对原图文字的抄录，也不表示已经生成过图像。

## 示例一：干燥晴天的县城修车铺，空镜

场景设定：北方县城临街修车铺，正午晴天，干燥无人，16:9。

中文提示词：一幅16:9横向纪实电影实拍感场景。正午晴天，北方县城一间敞开卷帘门的修车铺，空无一人。摄影机位于街对面的树荫边缘，平视中远景；前景是干燥的浅灰水泥路，屋檐投下边缘清晰的阴影，中景一辆待修轿车停在门内，后景工作台沿侧墙排开，保留车辆进入与人员走动的通道。常用扳手集中挂在工作台上方，轮胎靠墙收放，折好的抹布留在台边；油迹仅位于车辆下方的局部区域。来自左上方的直射日光照亮门口，室内主要由门洞反射光照明，暗处工具轮廓仍可辨。以水泥灰、旧蓝门板与轮胎黑为主色，金属、橡胶和混凝土具有不同的表面反应。空间自然、细节适量、正常透视、环境清晰，保持干燥明亮的正午状态。

English prompt: A single 16:9 horizontal scene with the photographic realism of an observational film. An empty roadside auto repair shop in a small northern Chinese town at noon on a clear, dry day. The camera stands across the street at the edge of tree shade, at eye level in a medium-wide view. Dry pale-gray concrete fills the foreground, crossed by the crisp shadow of the eaves. A car awaiting repair sits just inside the open roller door; a workbench runs along the side wall behind it, leaving a believable access route. Frequently used wrenches hang above the bench, tires are stored against the wall, and a folded rag rests on the edge. Oil marks remain localized beneath the car. Direct sunlight enters from the upper left; reflected light through the doorway keeps tools readable in the interior shade. Concrete gray, faded blue and tire black dominate, with distinct responses from metal, rubber and concrete. Natural perspective, readable surroundings, restrained detail, and a bright, dry midday atmosphere.

排除项：人物、雨雾、湿地面、满墙锈斑、青绿滤镜、夸张HDR、过锐纹理、字幕界面。

## 示例二：现代医院夜间走廊，干净无人

中文提示词：16:9横向单幅，具有纪实电影的实拍质感。一处现代医院夜间走廊，干净整洁，无人。摄影机放在走廊入口一侧，平视向远处转角拍摄；前景扶手与近侧门框限定观察位置，中景诊室门沿墙排列，后景为收起的护理推车和尽端窗，通道宽度保持真实、畅通。顶部线性灯具照亮地面和门框，墙面反射出柔和补光，窗外为暗蓝夜色；玻璃主要反射室内灯光，地面只有克制的柔亮反射。以灰白墙面、淡灰地材和少量蓝色导向标识为主，金属扶手、玻璃和地材保留各自不同的光泽。明暗过渡自然，环境细节清晰，气氛安静日常，维护良好的表面不添加破损或污渍。

English prompt: A single horizontal 16:9 scene with observational cinematic photographic realism. A clean, empty corridor in a modern hospital at night. The eye-level camera stands to one side of the entrance, looking toward a distant corner. A nearby doorframe and handrail establish the foreground; consultation room doors line the middle distance, while a stored nursing cart and an end window complete the background. Keep the passage open and realistically proportioned. Ceiling-mounted linear fixtures illuminate the floor and doorframes, with soft fill reflected by the walls. Dark blue night is visible outside the window; the glass mainly reflects interior fixtures. Floor reflections are subtle and softly defined. Gray-white walls, pale-gray flooring and a small amount of blue directional signage form the palette. Metal rails, glass and flooring retain distinct surface responses. Natural light transitions, readable environmental detail, an ordinary quiet atmosphere, and well-maintained surfaces without added damage or stains.

排除项：人物、恐怖气氛、废弃医院、烟雾、霉斑锈迹、积水、镜面地板、强青绿滤镜、文字覆盖层。

## 示例三：同场景改机位

输入：把修车铺改为门内向街外看，保留晴天和无人。

执行：沿用车辆、工作台与街道的空间关系，将摄影机移到可站立的门内位置；太阳仍在同一世界方向，不能随镜头切换而反向。室外更亮、门内较暗但可读；根据遮挡减少可见物件。重新编写该单一视角，不在同一提示词追加“同时从街对面看”。
