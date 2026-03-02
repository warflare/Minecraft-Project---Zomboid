# 1:1 Project Zomboid Minecraft Map

![finally](https://github.com/user-attachments/assets/75a44129-e72a-41c1-9ed0-df8df6844a44)

See the overview of this project here: [(https://youtu.be/X3W7WBX3BYs)]

Download my export of the world here: [(https://www.planetminecraft.com/project/project-zomboid-1-1-map-b42/)]

## Disclaimer
These scripts were written by ChatGPT. I only have a basic level of coding knowledge and am NOWHERE NEAR intelligent enough to write something like this.
Feel free to use this repo however you'd like.

**My challange to the internet: I hope to see the day somebody can make an even better version of this!**

## Python Summary
This repo contains the following **.py** files:
- `pz_b42_to_tmx.py`: takes the game map files located at `"C:\Program Files (x86)\Steam\steamapps\common\ProjectZomboid\media\maps\Muldraugh, KY"` and converts all tiles to .tmx format
  - `scan_tiles.py`: Scans all of the .tmx files from the output of `pz_b42_to_tmx.py` and outputs a `b42_tiles_used.csv` file that lists all of the tiles used on the map.
- `tsx_to_mapping_template.py`: This scans all TMX files and outputs a list of used tiles.
- `tmx_to_mc.py`: takes:
  - an existing minecraft world (I used a superflat)
  - some user-inputted args
  - `b42_tiles_used.csv` file
  - location of the .tmx files you want to export to the world

## Other Files
This repo also contains a `mapping_master.csv` file that I used as the "block dictonary" to build the world. 

My page contains a repo [https://github.com/pht122/Project-Zomboid-B42-Tile-Browser] that allows you to browse all of the tiles in Zomboid. 
I used this to quickly look at and assigned the tiles to respective Minecaft blocks.

## Used Command to Generate World
The powershell command I used to export the world on planet minecraft:
```
python "C:\Users\...\tmx_to_mc.py" `
>>    --tmx-dir "C:\...\TMX Files" `
>>    --world-dir "C:\Users\...\AppData\Roaming\.minecraft\saves\PZ B42" `
>>    --mapping-csv "C:\Users\...\mapping_master.csv" `
>>    --base-y 69
```
