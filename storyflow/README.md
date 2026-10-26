# Suno · StoryFlow — конвеєр відеокліпу «Ключі у траві»

Шот-лист у StoryFlow → кадри S01–S43 → анімація Veo 3.1 через RoyalTechno API → монтаж під трек Suno.

```
storyflow/
  veo/    скрипт генерації (rt_veo.py), промпти (scenes.json), frames/, clips/
  ui/     інтерфейс (у планах)
  docs/   нотатки пайплайну
```

## Запуск (Windows, Python 3.9+)
```
pip install requests pillow
cd storyflow\veo
:: створи rt_key.txt з API-ключем одним рядком (у git не потрапляє)
:: поклади кадри в frames\S01.png ... S43.png (16:9)
python rt_veo.py check
python rt_veo.py price
python rt_veo.py run --scenes S01,S02,S03
python rt_veo.py run --all
```
Ключ ніколи не комітити і не слати в чат.
