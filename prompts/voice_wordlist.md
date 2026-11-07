# 语音播报版 · 默认多音字词表

本文件是 `prompts/summary_voice.md`「多音字读法控制」一节的**默认词表，也是唯一真源**。
构造输入包时会作为 `wordlist` 字段整份注入，提示词本身不再内联词表。
后面要加词、删词，只改这个文件，不用动提示词。

## 使用前先看一眼

- 写拼音的原理是「跳过 TTS 前端的多音字打分，直接给音素」，只有支持这一能力的
  语音引擎才有效。**如果用的是阅读器自带的朗读**（读的是渲染后的文本），
  拼音串会被当成拉丁字母念出来，反而更糟；这种场景在输入包里写
  `本期词表：停用`，整节跳过，全部走中文改写兜底。
- 带调连写，全稿统一；不写数字调、不写括号注音、不写半截词。
- 本表只收高频、且确实容易读错的整词。词表越长，正文里夹的拼音越多，
  阅读体验越差，所以宁可少收，后面按实际朗读效果逐条补。

## 维护规则

- 每行一条，格式 `词语 → 带调连写拼音`，只收**整词**，不收单字。
- 拿不准调号就不要收，标错比不标更糟。
- 复合词整词写拼音，例 `人民银行 → rénmín yínháng`。
- 删掉某行 = 停用该词；新增一行 = 启用该词。分类只是给人看的，不影响匹配。
- 标题里不标拼音（见提示词规则），本表只作用于正文。

## 一、金融与市场

行业 → hángyè
行情 → hángqíng
银行 → yínháng
央行 → yāngháng
人民银行 → rénmín yínháng
运行 → yùnxíng
权重 → quánzhòng
重仓 → zhòngcāng
调仓 → tiáocāng
供给 → gōngjǐ
提供 → tígōng
换手率 → huànshǒulǜ
概率 → gàilǜ
效率 → xiàolǜ

## 二、通用高频（跨题材都会出现）

重要 → zhòngyào
重复 → chóngfù
重新 → chóngxīn
增长 → zēngzhǎng
上涨 → shàngzhǎng
涨幅 → zhǎngfú
中长期 → zhōngchángqī
长度 → chángdù
调节 → tiáojié
调查 → diàochá
定调 → dìngdiào
处理 → chǔlǐ
处境 → chǔjìng
应该 → yīnggāi
应对 → yìngduì
差别 → chābié
差不多 → chàbùduō
出差 → chūchāi
数量 → shùliàng
数据 → shùjù
数一数 → shǔyīshǔ
的确 → díquè
目的 → mùdì
得到 → dédào
为了 → wèile
成为 → chéngwéi
归还 → guīhuán
还有 → háiyǒu
几乎 → jīhū
几个 → jǐgè
更加 → gèngjiā
更正 → gēngzhèng
只有 → zhǐyǒu
少年 → shàonián
多少 → duōshǎo
教育 → jiàoyù
教书 → jiāoshū
会计 → kuàijì
会议 → huìyì
兴奋 → xīngfèn
高兴 → gāoxìng
方便 → fāngbiàn
便宜 → piányi
相同 → xiāngtóng
相貌 → xiàngmào
干净 → gānjìng
干部 → gànbù
音乐 → yīnyuè
可乐 → kělè

## 三、科技 / 医药 / 教育 / 法律（按题材偶发）

模型 → móxíng
模样 → múyàng
血液 → xuèyè
处方 → chǔfāng
冠心病 → guānxīnbìng
冠军 → guànjūn
背诵 → bèisòng
背景 → bèijǐng
量刑 → liàngxíng
测量 → cèliáng
勉强 → miǎnqiǎng
强大 → qiángdà
禁止 → jìnzhǐ
积累 → jīlěi
劳累 → láolèi
暴露 → bàolù

## 待观察（暂不启用，朗读实测有问题再加）

朝霞 → zhāoxiá
朝向 → cháoxiàng
参差 → cēncī
着陆 → zhuólù
着急 → zháojí
落枕 → làozhěn
折本 → shéběn
倔强 → juéjiàng
禁不住 → jīnbuzhù
露面 → lòumiàn
