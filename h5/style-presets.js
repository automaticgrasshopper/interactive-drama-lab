(function (root) {
  'use strict';

  root.SB_STYLE_PRESETS = [
    {
      id: 'smart_recommendation_v1',
      version: 1,
      name: '智能推荐',
      en: 'Smart Recommendation',
      summary: '根据剧本的题材、时代和氛围，自动从 8 套可执行画风中选择',
      colors: ['#0e7490', '#7c3aed', '#d97706'],
      isRecommendation: true
    },
    {
      id: 'filmic_realism_v1',
      version: 1,
      name: '电影级写实',
      en: 'Filmic Realism',
      summary: '真实皮肤与材质，自然宽容度，克制的电影光感',
      colors: ['#b8c0c7', '#59636d', '#242b31'],
      stylePrompt: 'filmic photorealism, physically plausible skin and materials, natural dynamic range, realistic optics, subtle sensor texture, motivated practical lighting, restrained sharpening and true-to-life color separation',
      videoTemporalPrompt: 'keep skin texture, material response, exposure curve and optical sharpness stable across frames',
      negativeStylePrompt: 'beauty-filter face replacement, luxury set redesign, added cinematic props, wardrobe change, artificial teal-orange wash',
      styleScope: 'rendering, materials, color response and lighting only',
      preserve: ['identity', 'age', 'face', 'hair', 'wardrobe', 'product geometry', 'location', 'action', 'camera'],
      intensity: 30
    },
    {
      id: 'neo_noir_v1',
      version: 1,
      name: '新黑色悬疑',
      en: 'Neo-Noir Thriller',
      summary: '深黑阶、青蓝与钠黄实景光，克制薄雾与轮廓',
      colors: ['#071018', '#14506a', '#d08b39'],
      stylePrompt: 'contemporary neo-noir surface treatment, deep neutral blacks, selective cyan and sodium-amber practical light, controlled haze, restrained film grain and sharp silhouette separation',
      videoTemporalPrompt: 'keep black level, practical-light color, haze density and grain stable across frames',
      negativeStylePrompt: 'automatic rain, fedora, trench coat, gun, neon sign, alley or detective office unless requested by content',
      styleScope: 'tonal palette, materials and lighting treatment only',
      preserve: ['story location', 'character identity', 'wardrobe', 'prop list', 'camera plan'],
      intensity: 34
    },
    {
      id: 'gothic_mystery_v1',
      version: 1,
      name: '哥特悬疑',
      en: 'Gothic Mystery',
      summary: '炭黑与冷靛色板，银色轮廓光，安静而不安',
      colors: ['#171620', '#343657', '#8d91a8'],
      stylePrompt: 'restrained gothic mystery, charcoal and cold-indigo palette, crisp dark shapes, overcast ambient light, controlled silver rim light, elegant line detail and quiet ominous atmosphere',
      videoTemporalPrompt: 'keep palette, edge treatment, contrast and rim-light character stable across frames',
      negativeStylePrompt: 'braided heroine, school uniform, raven, umbrella, gargoyle, cathedral or graveyard unless requested by content',
      styleScope: 'rendering, texture, palette relationship and lighting only',
      preserve: ['character design', 'current costume', 'exact location', 'action', 'shot'],
      intensity: 30
    },
    {
      id: 'retro_cinematic_film_v1',
      version: 1,
      name: '复古电影胶片',
      en: 'Retro Cinematic Film',
      summary: '80–90 年代电影胶片，柔和高光、暖光晕与有机颗粒',
      colors: ['#5b463c', '#b58a68', '#d8c3a5'],
      stylePrompt: 'late-1980s to 1990s theatrical film imaging, controlled fine organic film grain, gently faded colors, restrained saturation, subtle chemically plausible color drift, slightly lifted blacks, preserved shadow detail, soft highlight roll-off, subtle warm halation and soft vintage cinema-lens rendering without losing facial or object detail',
      videoTemporalPrompt: 'maintain consistent grain density, color response, contrast curve, optical softness and halation across all frames; allow natural frame-varying grain but prevent exposure flicker, color shifting, sharpness changes and unstable film damage; use only extremely subtle gate weave',
      negativeStylePrompt: 'VHS tracking lines, timestamps, film perforations, heavy scratches, severe dust damage, automatic period costumes, vintage vehicles, retro furniture, vintage packaging or historical props',
      styleScope: 'image texture, color response, optical softness and lighting only',
      preserve: ['character identity', 'wardrobe', 'props', 'location', 'action', 'composition', 'camera'],
      intensity: 36
    },
    {
      id: 'neo_chinese_wuxia_v1',
      version: 1,
      name: '新中式武侠',
      en: 'Neo-Chinese Wuxia',
      summary: '水墨明暗层级、青灰基调、矿物色点缀与留白',
      colors: ['#29383e', '#6d7c78', '#a77b50'],
      stylePrompt: 'restrained neo-Chinese cinematic rendering, ink-informed tonal hierarchy, charcoal blue-gray base, sparse mineral-color accents, natural cloth and weathered wood texture, soft mist diffusion, elegant negative space and controlled high-contrast silhouette',
      videoTemporalPrompt: 'keep ink tonal hierarchy, mineral-color accents, mist density and material texture stable across frames',
      negativeStylePrompt: 'automatic hanfu redesign, sword, bamboo forest, palace, lantern, calligraphy, dragon or flying pose unless requested by content',
      styleScope: 'rendering, tonal hierarchy, materials, palette and lighting only',
      preserve: ['dynasty setting', 'costume ID', 'weapon ID', 'location ID', 'action', 'character identity'],
      intensity: 28
    },
    {
      id: 'cel_anime_v1',
      version: 1,
      name: '赛璐璐动漫',
      en: 'Cel Anime',
      summary: '干净墨线、平涂底色、二阶阴影与稳定线宽',
      colors: ['#405a78', '#d9b15d', '#f1e8d2'],
      stylePrompt: 'clean 2D cel animation, precise ink contour, flat base colors, two-step cel shading, restrained specular highlights, stable line weight and readable facial features',
      videoTemporalPrompt: 'keep line weight, shadow steps, facial drawing and palette stable across frames',
      negativeStylePrompt: 'school uniform, shrine, cherry blossoms, fantasy weapon, exaggerated eye redesign or hairstyle change unless requested by content',
      styleScope: 'rendering, linework, shading, palette and lighting only',
      preserve: ['face shape', 'eye color', 'hair shape', 'costume ID', 'body proportion', 'location', 'pose', 'camera'],
      intensity: 32
    },
    {
      id: 'stylized_3d_v1',
      version: 1,
      name: '风格化 3D 奇幻',
      en: 'Stylized 3D Fantasy',
      summary: '柔和雕塑体积、可信材质、绘画化环境与暖色反弹光',
      colors: ['#594d7c', '#b26f68', '#deb676'],
      stylePrompt: 'stylized 3D animation, simplified but believable materials, soft sculpted forms, expressive silhouette, controlled subsurface scattering, painterly environment detail, warm cinematic bounce light and clean readable shapes',
      videoTemporalPrompt: 'keep geometry abstraction, material response, subsurface scattering and palette stable across frames',
      negativeStylePrompt: 'wizard hat, magic staff, castle, glowing rune, character-species change or toy-like body proportion unless requested by content',
      styleScope: 'geometry abstraction, material rendering, palette and lighting only',
      preserve: ['character species', 'face', 'body ratio', 'costume', 'prop', 'environment', 'action', 'camera'],
      intensity: 30
    },
    {
      id: 'pixel_narrative_v1',
      version: 1,
      name: '像素叙事',
      en: 'Pixel Narrative',
      summary: '固定整数像素网格、24 色主色板、清晰轮廓与有序抖动',
      colors: ['#17223a', '#3e6b62', '#d19a55'],
      stylePrompt: 'narrative pixel art on a fixed integer grid, nearest-neighbor edges, limited 24-color master palette, crisp silhouettes, selective ordered dithering and readable sprite-scale facial clusters',
      videoTemporalPrompt: 'keep pixel grid, palette, dithering pattern and sprite-scale detail stable across frames',
      negativeStylePrompt: 'subpixel lines, vector smoothing, soft blur, added gameplay objects or automatic UI redesign',
      styleScope: 'rendering and asset grid only',
      preserve: ['collision shape', 'tile layout', 'interaction hotspot', 'UI information', 'character identity'],
      intensity: 28,
      assetRules: { baseResolution: '320x180', scale: 'integer only', antiAliasing: false, tileSize: '16 or 32 px' }
    }
  ];
})(window);
