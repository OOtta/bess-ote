# BESS OTE kalkulačka

Kdy nabíjet, vybíjet nebo čekat s domácí baterií (BESS) podle spotových cen denního trhu
**OTE** – a kolik by to vydělalo v minulosti.

[![CI](https://github.com/OOtta/bess-ote/actions/workflows/ci.yml/badge.svg)](https://github.com/OOtta/bess-ote/actions/workflows/ci.yml)

Dvě hlavní funkce:

1. **`bess-ote plan`** – z kapacity a výkonu baterie, aktuálního stavu nabití (SoC) a vašich
   tarifů spočítá optimální rozvrh na všechny **právě zveřejněné** ceny OTE (dnešek, po ~13:05
   i zítřek). Vypíše buď jen akci na příští čtvrthodinu (`--next`), nebo celý interval.
2. **`bess-ote backtest`** – simulace na historických cenách. V každé čtvrthodině zná jen ceny,
   které už byly v tu chvíli zveřejněné, rozhodne **stejným algoritmem jako `plan`**, provede
   první krok a pokračuje. Výsledkem je zisk, nakoupená/prodaná energie, cykly, rozpis po
   dnech/měsících a srovnání s ideálem (dokonalá znalost budoucnosti).

Nákup i výkup se počítá ze spotové ceny podle vašeho vzorce (násobek + přirážka, zvlášť pro
záporné ceny), distribuční poplatky VT/NT a další poplatky se připočítávají **jen k nakoupené
energii**, DPH zvlášť pro nákup a prodej. Žádné skryté výchozí hodnoty: co nezadáte, je
neutrální (násobek 1, přirážka 0, distribuce 0, DPH 0) a použitý vzorec se vždy vypíše.

## Obsah

- [Instalace](#instalace)
- [Rychlý start](#rychlý-start)
- [Příkazy a volby](#příkazy-a-volby)
- [Jak se počítá cena](#jak-se-počítá-cena)
  - [Záporné ceny (`--buy-negative`, `--sell-negative`)](#záporné-ceny---buy-negative---sell-negative)
  - [Měsíční poplatky](#měsíční-poplatky)
- [Algoritmus](#algoritmus)
  - [Mřížka SoC a plný výkon](#mřížka-soc-a-plný-výkon)
  - [Rychlost a paralelní výpočet (`--jobs`)](#rychlost-a-paralelní-výpočet---jobs)
- [Zveřejňování cen OTE a backtest](#zveřejňování-cen-ote-a-backtest)
- [Výstupy](#výstupy)
  - [Rozpis po hodinách a čtvrthodinách](#rozpis-po-hodinách-a-čtvrthodinách)
- [Konfigurační soubor](#konfigurační-soubor)
- [Data a mezipaměť](#data-a-mezipaměť)
- [Omezení](#omezení)
- [Vývoj](#vývoj)
- [Licence](#licence)

## Instalace

Potřebujete Python 3.11 nebo novější.

```bash
pipx install "git+https://github.com/OOtta/bess-ote"
# nebo
pip install "git+https://github.com/OOtta/bess-ote"
```

Závislosti: `requests`, `numpy` a `tzdata` (databáze časových pásem pro Windows a Termux).

**Termux (Android):** numpy se tam z pipu kompiluje, rychlejší je systémový balíček:

```bash
pkg install python python-numpy
pipx install --system-site-packages "git+https://github.com/OOtta/bess-ote"
```

## Rychlý start

```bash
# Co dělat v příští čtvrthodině: baterie 10 kWh / 5 kW, teď nabitá na 40 %
bess-ote plan -c 10 -p 5 --soc 40 --next

# Celý rozvrh s tarify: nákup spot + 400 Kč/MWh s 21 % DPH, výkup spot − 300 Kč/MWh,
# distribuce VT 2,435 / NT 0,416 Kč/kWh s vlastními časy NT
bess-ote plan -c 10 -p 5 --soc 40 \
  -a 400 --buy-vat 21 --sell-add -300 \
  --distribution-high 2.435 --distribution-low 0.416 \
  --low-tariff-hours "1:15-5:15,12:30-16:30" \
  --low-tariff-hours-weekend "2:45-6:45,13:00-17:00" --holidays weekend

# Jak by plán vypadal v daný okamžik (jen s cenami zveřejněnými do té doby)
bess-ote plan -c 10 -p 5 --soc 30 --at "2026-11-21 13:20" -o plan.csv

# Backtest za červen–říjen 2026 se stejnými tarify a měsíčními poplatky, rozpis po měsících
bess-ote backtest -c 10 -p 5 --soc-min 10 --from 2026-06 --to 2026-10 -b month \
  -a 400 --buy-vat 21 --sell-add -300 --buy-monthly-fee 147 --distribution-monthly-fee 257 \
  --distribution-high 2.435 --distribution-low 0.416 \
  --low-tariff-hours "1:15-5:15,12:30-16:30" \
  --low-tariff-hours-weekend "2:45-6:45,13:00-17:00" --holidays weekend
```

Ukázka výstupu `plan --next`:

```text
Plan as of 2026-11-22 11:07 CET: 2026-11-22 11:15 CET – 2026-11-23 00:00 CET (51 quarter-hours)
NEXT 11:15–11:30  WAIT, SoC 50.0 % → 50.0 %
```

Ukázka výstupu `backtest` (skutečné ceny OTE, parametry viz poslední příklad výše, s měsíčními
poplatky 147 + 257 Kč bez DPH):

```text
Backtest 2026-06-01 – 2026-10-31 (153 days)
Battery:      10 kWh, 5 kW, round trip 90.0 % (charge 94.9 %, discharge 94.9 %), SoC 10–100 %
Buy:          spot + 400 CZK/MWh, VAT 21 %
Sell:         spot − 300 CZK/MWh, excl. VAT
Distribution: VT 2.435 / NT 0.416 CZK/kWh (NT 01:15–05:15, 12:30–16:30; Sat/Sun 02:45–06:45, 13:00–17:00; holidays as weekend) (on purchase only)
Prices:       prices for day D known on D−1 at 12:57 (delivery ≤ 2025-09-30) / 13:05 (from 2025-10-01), used from the next quarter-hour; 154 re-optimisations

Bought             1314.93 kWh   cost          3197.08 CZK
Sold               1183.44 kWh   revenue       6523.35 CZK
Trading profit                           3326.27 CZK (21.74 CZK/day, before monthly fees)
Monthly fees                            -2444.20 CZK (supplier 147 + distribution 257 CZK/month + VAT 21 % = 488.84 CZK/month, prorated by days)
NET PROFIT                                882.07 CZK (5.77 CZK/day)
Cycles               138.6 (equivalent full cycles of usable capacity)
SoC           10.0 % → 10.0 % (stored energy change worth 0.00 CZK; trading profit incl. it 3326.27 CZK)
Perfect foresight (all prices known in advance): trading profit 3334.61 CZK incl. stored energy, 138.7 cycles; rolling plan captures 99.7 %

month       bought kWh       cost   sold kWh    revenue     profit      fees        net  cycles
2026-06         244.76     418.48     220.28    1214.13     795.66    488.84     306.82    25.8
2026-07         278.92     474.15     251.03    1050.26     576.10    488.84      87.26    29.4
2026-08         248.89     571.09     222.97    1176.30     605.21    488.84     116.37    26.1
2026-09         326.25     914.60     294.66    1835.78     921.18    488.84     432.34    34.5
2026-10         216.11     818.77     194.50    1246.89     428.12    488.84     -60.72    22.8
```

Výstup programu je v angličtině (čísla s desetinnou tečkou, snadno se dále zpracují),
dokumentace v češtině.

## Příkazy a volby

| Příkaz | Co dělá |
|---|---|
| `bess-ote plan` | optimální rozvrh od příští čtvrthodiny do konce zveřejněných cen; `--next` jen příští akce |
| `bess-ote backtest --from … --to …` | klouzavá simulace na historických cenách + srovnání s ideálem |

Všechny volby vypíše `bess-ote plan --help` / `bess-ote backtest --help`. Volby jsou společné
pro oba příkazy (kromě posledních dvou tabulek).

**Baterie**

| Volba | Význam | Výchozí |
|---|---|---|
| `-c`, `--capacity KWH` | využitelná kapacita (100 % SoC) | povinné |
| `-p`, `--power KW` | max. nabíjecí výkon (na straně sítě, AC) | povinné |
| `--discharge-power KW` | max. vybíjecí výkon | = `--power` |
| `--efficiency PCT` | účinnost celého cyklu (round trip), rozdělí se rovnoměrně na nabíjení a vybíjení | 90 |
| `--charge-efficiency PCT`, `--discharge-efficiency PCT` | účinnost zvlášť (síť → baterie, baterie → síť) | z `--efficiency` |
| `--soc-min PCT`, `--soc-max PCT` | povolený rozsah nabití | 0, 100 |
| `--soc PCT` | aktuální SoC (plan) / počáteční SoC (backtest) | = `--soc-min` |
| `--cycle-cost KČ_KWH` | opotřebení: náklad na každou kWh dodanou z baterie do sítě | 0 |
| `--terminal value\|zero\|min-soc` | co s energií, která zbude na konci známých cen – viz [Algoritmus](#algoritmus) | `value` |
| `--terminal-price KČ_KWH` | prodejní cena zbylé energie pro `value` | průměrná výkupní cena posledního známého dne |
| `--terminal-soc PCT` | minimální SoC na konci pro `min-soc` | = `--soc` |
| `--soc-step KWH` | jemnost mřížky SoC optimalizátoru, viz [Mřížka SoC](#mřížka-soc-a-plný-výkon) | zlomek čtvrthodiny na plný výkon, nejvýš kapacita / 400 |

**Nákup (spot × násobek + přirážka)**

| Volba | Význam | Výchozí |
|---|---|---|
| `-m`, `--buy-multiplier X` | násobek spotové ceny | 1 |
| `-a`, `--buy-add KČ_MWH` | přirážka dodavatele | 0 |
| `--buy-negative-multiplier X`, `--buy-negative-add KČ_MWH` | jiný vzorec pro záporné spotové ceny | = normální |
| `--buy-negative as-is\|clamp-spot\|clamp-total` | zacházení se zápornými cenami, viz [Záporné ceny](#záporné-ceny---buy-negative---sell-negative) | `as-is` |
| `--buy-vat PCT` | DPH na nákup (energie + distribuce + poplatky, včetně měsíčních) | 0 |
| `--buy-monthly-fee KČ` | stálá měsíční platba dodavateli bez DPH, viz [Měsíční poplatky](#měsíční-poplatky) | 0 |

**Výkup (spot × násobek + přirážka)**

| Volba | Význam | Výchozí |
|---|---|---|
| `--sell-multiplier X` | násobek | 1 |
| `--sell-add KČ_MWH` | přirážka, obvykle záporná = poplatek výkupce (např. `-300`) | 0 |
| `--sell-negative-multiplier`, `--sell-negative-add`, `--sell-negative` | jako u nákupu, viz [Záporné ceny](#záporné-ceny---buy-negative---sell-negative) | = normální |
| `--sell-vat PCT` | DPH přičtená k tržbě (jen plátci DPH) | 0 |

**Distribuce a poplatky (sazby za kWh jen na nakoupenou energii)**

| Volba | Význam | Výchozí |
|---|---|---|
| `--distribution-high KČ_KWH` | distribuce ve VT (nebo jediná sazba), bez DPH | 0 |
| `--distribution-low KČ_KWH` | distribuce v NT, bez DPH (vyžaduje časy NT) | – |
| `--distribution-monthly-fee KČ` | stálé měsíční platby za distribuci bez DPH (jistič / rezervovaný příkon …), viz [Měsíční poplatky](#měsíční-poplatky) | 0 |
| `--purchase-fee KČ_KWH` | další poplatky za kWh bez DPH (systémové služby, POZE, daň z elektřiny …) | 0 |
| `--low-tariff-hours "1:15-5:15,12:30-16:30"` | časy NT v pracovní dny (mohou přes půlnoc) | – |
| `--low-tariff-hours-weekend "…"` | časy NT o víkendu | = pracovní dny |
| `--holidays weekday\|weekend` | státní svátky jako pracovní den, nebo jako víkend | `weekday` |

**Obecné**

| Volba | Význam |
|---|---|
| `--publish-time HH:MM` | kdy jsou ceny na den D známé (místní čas dne D−1); výchozí 12:57 / 13:05 – viz níže |
| `--json` | na stdout jen jeden JSON dokument, varování uvnitř (`warnings`), nic na stderr |
| `--config FILE` | konfigurační soubor TOML (také `BESS_OTE_CONFIG`) |
| `--cache-dir DIR`, `--no-cache`, `--refresh-cache` | mezipaměť cen – viz [Data a mezipaměť](#data-a-mezipaměť) |

**`plan`**

| Volba | Význam |
|---|---|
| `--next` | vypsat jen akci na příští čtvrthodinu |
| `--at "RRRR-MM-DD HH:MM"` | plán k danému místnímu času; použijí se jen ceny zveřejněné do té doby. Bez `--at` = teď, se vším, co OTE už zveřejnil |
| `-o FILE` | rozvrh do `.csv` nebo `.json` |
| `-b hour\|quarter-hour` | rozpis: `hour` (alias `h`, `60min`) = tabulka sečtená po hodinách, `quarter-hour` (alias `qh`, `15min`, `quarter`) = po čtvrthodinách; v JSON navíc pole `breakdown`. Bez volby se vypíše běžná čtvrthodinová tabulka – viz [Rozpis po hodinách a čtvrthodinách](#rozpis-po-hodinách-a-čtvrthodinách) |
| `--breakdown-csv FILE` | řádky rozpisu do CSV (bez `-b` čtvrthodiny rozvrhu) |

**`backtest`**

| Volba | Význam |
|---|---|
| `--from DATUM`, `--to DATUM` | období (`RRRR-MM-DD` nebo `RRRR-MM` = celý měsíc), nejpozději do včerejška |
| `-b month\|day\|hour\|quarter-hour\|none` | rozpis po měsících, dnech (`d`), hodinách (`h`, `60min`) nebo čtvrthodinách (`qh`, `15min`, `quarter`); **výchozí `none`** = jen souhrn – viz [Rozpis po hodinách a čtvrthodinách](#rozpis-po-hodinách-a-čtvrthodinách) |
| `--breakdown-csv FILE` | řádky zvoleného rozpisu do CSV |
| `--no-foresight` | nepočítat srovnání s dokonalou znalostí budoucnosti |
| `-j`, `--jobs N` | počet paralelních procesů pro výpočet (výchozí `auto` = počet jader CPU, `1` = sériově); výsledek je pro každé N **stejný** – viz [Rychlost a paralelní výpočet](#rychlost-a-paralelní-výpočet---jobs) |
| `--no-progress` | nezobrazovat průběh výpočtu (viz níže) |
| `-o FILE` | všechny čtvrthodiny do `.csv`, nebo vše (souhrn, rozpis, čtvrthodiny) do `.json` |

**Průběh výpočtu.** Backtest delšího období trvá i desítky sekund, proto během výpočtu
ukazuje na stderr jednoduchý řádek s procenty, počtem zpracovaných dní a odhadem zbývajícího
času:

```text
[##########--------------]  41.7 %  64/153 days        ETA 0:12
```

Řádek se překresluje znakem `\r` (jen ASCII, funguje i ve Windows a Termuxu, bez dalších
závislostí) a po skončení se smaže. Zobrazí se **jen když je stderr terminál** a není zadáno
`--json` – při přesměrování do souboru, v rouře nebo cronu se nic nevypisuje. Standardní
výstup ani soubor z `-o` neovlivní. Vypnout jde volbou `--no-progress`. Procenta odpovídají hotové práci optimalizátoru –
přepočtům klouzavého plánu a výpočtu ideálu, váženým počtem čtvrthodin – ať běží za sebou,
nebo paralelně. Po dokončení pruh vždy ukáže 100 %; odhad zbývajícího času se zobrazí až po
prvních procentech výpočtu.

## Jak se počítá cena

Pro každou čtvrthodinu se spotovou cenou `spot` (Kč/MWh):

```text
nákup  [Kč/kWh] = ( (spot × m + a) / 1000 + distribuce(VT/NT) + poplatky ) × (1 + DPH nákup)
výkup  [Kč/kWh] = ( spot × m_v + a_v ) / 1000 × (1 + DPH výkup)
```

- Pro **záporný spot** platí vlastní koeficienty a režimy `--buy-negative` /
  `--sell-negative` – viz [Záporné ceny](#záporné-ceny---buy-negative---sell-negative).
- Distribuce se podle časů NT počítá po čtvrthodinách v místním čase (správně i ve dnech
  přechodu na letní/zimní čas); čtvrthodina, která NT protíná jen částí, dostane vážený průměr.
- **Měsíční paušály** (jistič, poplatek dodavateli) se zadávají zvlášť a rozhodování
  neovlivní – viz [Měsíční poplatky](#měsíční-poplatky).
- Ceny OTE: do 30. 9. 2025 hodinové ceny v Kč (každá platí pro 4 čtvrthodiny), od 1. 10. 2025
  čtvrthodinové ceny v EUR přepočtené kurzem ČNB platným pro den dodávky.

### Záporné ceny (`--buy-negative`, `--sell-negative`)

Při záporné spotové ceně OTE se smlouvy dodavatelů i výkupců liší, proto se nákup i výkup
nastavuje zvlášť, ve dvou krocích:

1. **Které koeficienty platí.** Pro čtvrthodinu se záporným spotem (ostře pod 0) se použije
   `--buy-negative-multiplier` / `--buy-negative-add` (u výkupu `--sell-negative-multiplier` /
   `--sell-negative-add`); co není zadáno, převezme se z běžného násobku / přirážky. O výběru
   rozhoduje znaménko **původní** ceny OTE – ještě před jakýmkoli ořezáním.
2. **Jak se se zápornou cenou naloží** (`--buy-negative`, `--sell-negative`, v konfiguraci
   `negative` v sekci `[buy]` / `[sell]`):
   * `as-is` (výchozí) – záporný spot se použije tak, jak je; po vynásobení a přičtení
     přirážky může vyjít záporná cena;
   * `clamp-spot` – záporný spot se **nejdřív nahradí nulou** a teprve pak se použijí
     koeficienty, platí tedy jen přirážka;
   * `clamp-total` – cena se spočítá s koeficienty a **výsledek se ořízne zdola na 0**.

**Nákup** (`--buy-negative`) – co znamená pro vás jako kupujícího:

* `as-is` – záporná nákupní cena = **za nákup (nabíjení) dostanete zaplaceno**; optimalizátor
  v takové čtvrthodině rád nabíjí;
* `clamp-spot` – zaplatíte jen přirážku (spot −500, přirážka +400 → 400 Kč/MWh);
* `clamp-total` – za nákup nikdy nedostanete zaplaceno, ale může být levnější než přirážka
  (−500 + 400 = −100 → 0; −200 + 400 = 200).

Ceny energie v Kč/MWh bez DPH a bez distribuce (distribuce a poplatky se k nákupu připočítají
vždy):

| Spot OTE | Nákup: koeficienty | `as-is` | `clamp-spot` | `clamp-total` |
|---:|---|---:|---:|---:|
| 1 000 | × 1, + 400 | 1 400 | 1 400 | 1 400 |
| −200 | × 1, + 400 | 200 | 400 | 200 |
| −500 | × 1, + 400 | −100 | 400 | 0 |
| −500 | × 1,15, + 400 | −175 | 400 | 0 |
| −500 | × 1,15, + 400; při záporném spotu × 1, + 0 | −500 | 0 | 0 |

**Výkup** (`--sell-negative`) – z pohledu prodávajícího (přirážka u výkupu bývá záporná =
poplatek výkupce, např. `--sell-add -300`):

* `as-is` – záporná výkupní cena = **za prodej (vybíjení do sítě) platíte**; optimalizátor
  v takové čtvrthodině nevybíjí;
* `clamp-spot` – záporný spot se bere jako 0, zůstane jen přirážka; se záporným poplatkem
  výkupce tedy za prodej stále platíte (−300 Kč/MWh);
* `clamp-total` – výsledek se ořízne na 0: **za prodej nikdy neplatíte**, při záporné ceně
  prostě nedostanete nic.

| Spot OTE | Výkup: koeficienty | `as-is` | `clamp-spot` | `clamp-total` |
|---:|---|---:|---:|---:|
| 1 000 | × 1, − 300 | 700 | 700 | 700 |
| 200 | × 1, − 300 | −100 | −100 | 0 |
| −200 | × 1, − 300 | −500 | −300 | 0 |
| −500 | × 1, − 300 | −800 | −300 | 0 |
| −500 | × 1, − 300; při záporném spotu × 1, + 0 | −500 | 0 | 0 |

`clamp-total` ořezává každou zápornou výslednou cenu, tedy i kladný spot pod výší poplatku
(řádek se spotem 200), `clamp-spot` jen záporný spot. Kladné ceny nad poplatkem režim nijak
neovlivní. Jak to dělá váš dodavatel / výkupce, najdete ve smlouvě nebo v ceníku (obvykle
u definice ceny pro „hodiny se zápornou cenou“). Stejná pravidla používá
[egd-api-client](https://github.com/OOtta/egd-api-client).

### Měsíční poplatky

Stálé měsíční platby nezávislé na odebrané energii:

* `--buy-monthly-fee KČ` (v konfiguraci `[buy] monthly_fee`) – stálá platba dodavateli
  (paušál obchodníka),
* `--distribution-monthly-fee KČ` (v konfiguraci `[distribution] monthly_fee`) – stálé platby
  za distribuci (za jistič / rezervovaný příkon, případně další měsíční složky).

Obě částky se zadávají **bez DPH v Kč za měsíc** a obě se řídí **`--buy-vat`** (jsou součástí
faktury za odběr). Příklad: `--buy-monthly-fee 147 --distribution-monthly-fee 257 --buy-vat 21`
= (147 + 257) × 1,21 = **488,84 Kč/měsíc**.

* **Rozhodování neovlivní.** Jsou to fixní náklady – platíte je stejně, ať baterie nabíjí,
  vybíjí, nebo stojí – takže optimalizátor dává s poplatky i bez nich přesně stejný rozvrh
  (ověřuje test). Slouží k tomu, abyste viděli čistý výsledek.
* **Rozpočítání po dnech.** Každý den nese `poplatek / počet dní daného měsíce` (v červnu
  1/30, v červenci 1/31, …); neúplný měsíc se tedy počítá poměrně podle dní (např. 25.–30. 6.
  = 6/30 měsíčního poplatku). Den se dál rovnoměrně dělí na čtvrthodiny (sloupec `fees_czk`
  v CSV; ve dnech změny času 92 / 100 čtvrthodin). Poplatek se počítá za **všechny dny
  období**, i za dny bez cen OTE.
* **Backtest** vypíše *Trading profit* (zisk z obchodování před poplatky), *Monthly fees*
  a *NET PROFIT* (čistý zisk po poplatcích); rozpis po měsících/dnech má sloupce `fees`
  a `net`, JSON pole `fees_czk`, `net_profit_czk` a `net_profit_per_day_czk`. Srovnání
  s ideálem se dělá na zisku z obchodování (poplatky jsou v obou případech stejné).
* **Plán** připočte poplatek poměrně za délku horizontu (např. od 12:00 do půlnoci = půl dne)
  a vypíše čistý výsledek; v JSON jako `summary.fees_czk` a `summary.net_profit_czk`.

## Algoritmus

Optimalizace je **dynamické programování** přes jemnou mřížku uložené energie (výchozí krok
viz [Mřížka SoC](#mřížka-soc-a-plný-výkon), např. 0,0165 kWh u 10 kWh / 5 kW baterie):

- stav = energie v baterii, akce v každé čtvrthodině = jedna změna energie (nabíjet X kW,
  vybíjet X kW, nebo čekat) – **současné nabíjení a vybíjení je vyloučeno**;
- omezení: výkon na straně sítě (nabíjení `P × 0,25 h × η_nab`, vybíjení `P × 0,25 h / η_vyb`
  uložené energie), min./max. SoC;
- náklad nabíjení = nákupní cena × energie ze sítě, výnos vybíjení = (výkupní cena −
  opotřebení) × energie do sítě; čtvrthodina bez ceny OTE (stav nouze) = jen čekání;
- energie zbylá na konci horizontu (`--terminal`):
  - `value` (výchozí) – každá uložená kWh má hodnotu `výkupní cena × η_vyb − opotřebení`, kde
    výkupní cena je průměr **posledního známého dne** (nebo `--terminal-price`). Baterie se tak
    nevybíjí „do nuly“ jen proto, že další ceny ještě nejsou známé;
  - `zero` – zbylá energie nemá žádnou hodnotu;
  - `min-soc` – na konci musí být SoC alespoň `--terminal-soc`.

Výsledek je **přesné optimum na dané mřížce** (testy ho ověřují proti úplnému prohledání všech
posloupností akcí). Proč ne lineární programování: LP by vyžadovalo scipy (velká závislost,
v Termuxu problematická) a vyloučení současného nabíjení a vybíjení by z něj udělalo úlohu
s celočíselnými proměnnými. DP v numpy spočítá den a půl (≈140 čtvrthodin) za několik
milisekund.

### Mřížka SoC a plný výkon

Optimalizátor pracuje s energií v násobcích kroku mřížky, takže i změna energie za jednu
čtvrthodinu musí být celý počet kroků. Výchozí krok je **zlomek energie, kterou baterie uloží
za čtvrthodinu na plný výkon, nebo kapacita / 400 – podle toho, co je menší**:

```text
krok = (P_nab × 0,25 h × η_nab) / (K × 2^j)
```

- `K` závisí jen na výkonu a účinnostech: nejmenší K ≥ 9, pro které je celým počtem kroků
  i **plné vybíjení** (při výchozí účinnosti 90 % tam a zpět vyjde K = 9: plné nabití = 9
  kroků, plné vybití = 10 kroků, obojí přesně); jinak takové, které ztratí nejvýš 0,5 %
  výkonu vybíjení;
- `2^j` je nejmenší mocnina dvou, se kterou je krok ≤ kapacita / 400 (jemné rozlišení SoC
  u malých baterií);
- mřížka začíná přesně na minimálním SoC.

Plné nabíjení je tak **vždy přesně zadaný výkon** a mřížky jsou do sebe vnořené: krok větší
baterie dělí krok menší baterie, takže větší baterie má k dispozici každý rozvrh menší
baterie a **zisk při dokonalé znalosti cen s kapacitou nikdy neklesá** (ověřují testy na
hustém rozsahu kapacit, stejně tak blízkost k optimu lineárního programu – rozdíl do 0,5 %,
u větších baterií prakticky nulový).

*Proč ta změna (verze 0.1.0 před opravou):* krok býval kapacita / 400 bez ohledu na výkon.
U velkých baterií pak plný výkon nebyl celým počtem kroků a zaokrouhlil se dolů – u 175 kWh /
5 kW byl krok 0,4375 kWh, nabíjení 2 kroky = 0,875 kWh místo 1,186 kWh, tedy **3,7 kW místo
5 kW** (u 100 kWh 4,2 kW, u 125 kWh 3,95 kW). Zisk proto s rostoucí kapacitou nepravidelně
klesal. Backtest červen–říjen 2026 (5 kW, ostatní parametry z příkladu výše):

| Kapacita | ideál před | ideál po | klouzavý plán před | klouzavý plán po |
|---:|---:|---:|---:|---:|
| 10 kWh (2×) | 3 327,94 | 3 334,61 | 3 319,54 | 3 326,27 |
| 25 kWh (5×) | 5 117,76 | 5 260,47 | 5 059,43 | 5 207,76 |
| 50 kWh (10×) | 5 575,47 | 5 836,54 | 5 125,64 | 5 402,90 |
| 75 kWh (15×) | 5 979,55 | 6 150,00 | 5 236,82 | 5 402,90 |
| 100 kWh (20×) | 5 712,49 | 6 368,87 | 4 792,78 | 5 402,90 |
| 125 kWh (25×) | 5 643,60 | 6 517,04 | 4 615,92 | 5 402,90 |
| 150 kWh (30×) | 6 082,82 | 6 633,08 | 4 895,77 | 5 402,90 |
| 175 kWh (35×) | 5 713,86 | 6 732,01 | 4 521,71 | 5 402,90 |

(Kč za obchodování, ideál včetně hodnoty energie v baterii na konci.) Klouzavý plán od
~45 kWh dál neroste: plánuje nanejvýš den a půl dopředu a zbylou energii oceňuje průměrnou
cenou posledního známého dne, takže 5 kW baterie nikdy nepotřebuje uložit víc než ~45 kWh –
větší kapacita zůstane nevyužitá, ale zisk nesníží. Ideál zná celé období dopředu a velkou
kapacitu využije k přesunu energie mezi dny.

Daň za přesnost je rychlost: jemnější mřížka znamená víc stavů a akcí. U 10 kWh / 5 kW je
práce na krok ≈ 2,6× větší než dřív (608 stavů × 153 akcí); u velkých baterií se krok drží
na 1/9 plného výkonu (175 kWh: 1 329 stavů × 20 akcí). Hrubší a rychlejší mřížku lze zvolit
ručně `--soc-step`. U velmi dlouhých období s velkou baterií ideál nedrží celou tabulku
rozhodnutí v paměti, ale ukládá jen funkci hodnoty po úsecích a rozhodnutí úseku dopočítá
(stejná čísla, ≈ 2× víc výpočtu, zlomek paměti).

### Rychlost a paralelní výpočet (`--jobs`)

Backtest se skládá z mnoha nezávislých úloh. **Zpětný průchod DP** (funkce hodnoty a nejlepší
akce pro každý stav) každého přepočtu závisí jen na cenách známých v okamžiku zveřejnění –
**ne na stavu baterie**. Všechny přepočty (≈ jeden za den) i výpočet ideálu se proto dají
spočítat současně; potom se jen postupně, čtvrthodinu po čtvrthodině, přenese SoC přes
uložené tabulky rozhodnutí (dopředný průchod). Výsledek je tak **bit po bitu stejný** jako při
sériovém výpočtu i jako v předchozí verzi programu (testy porovnávají každou čtvrthodinu
včetně všech čísel; ověřeno i na skutečných cenách červen–říjen 2026 – CSV i JSON jsou bajt
po bajtu shodné).

- `--jobs auto` (výchozí) použije tolik procesů, kolik má počítač jader; přepočty běží
  v samostatných procesech, výpočet ideálu ve vlákně hlavního procesu. U krátkých období
  (méně než ~1 500 čtvrthodin práce na proces) se paralelismus nevyplatí a výpočet běží
  sériově. `--jobs 1` vynutí sériový výpočet.
- Procesy, ne vlákna: jeden krok DP je malá operace numpy (≈ 36 000 čísel), během níž drží
  Python zámek GIL – vlákna zrychlila jen asi 2×, procesy škálují s počtem jader (měřeno).
- Windows (spouštění procesů metodou *spawn*) funguje; kde procesy vytvořit nelze (např.
  Termux / Android bez funkčního `sem_open`), program tiše počítá sériově – se stejným
  výsledkem.
- `plan` řeší jedinou úlohu (≈ 50–140 čtvrthodin) za několik milisekund; většinu z ~0,2 s
  zabere start Pythonu, paralelismus tu nemá co zrychlit.

Další zrychlení bez vlivu na výsledek: časy zveřejnění se počítají jednou na den (dříve pro
každý den knihy cen v každé čtvrthodině), cena pro ocenění zbylé energie se bere
z předpočítaného rozpisu po dnech, nákupní a výkupní ceny se spočítají jednou pro celé období
a smyčka DP používá předem alokované pole (float64, bez alokací v každém kroku).

Měření (backtest červen–říjen 2026 z příkladu výše, ceny v mezipaměti, 8 jader, celý běh
programu včetně startu Pythonu):

| | `--jobs 1` | `--jobs 2` | `--jobs 4` | `--jobs auto` (8) |
|---|---:|---:|---:|---:|
| backtest se srovnáním s ideálem | 5,30 s | 3,47 s | 2,48 s | 2,36 s |
| backtest `--no-foresight` | 3,34 s | 1,89 s | 1,12 s | 0,76 s |

(S jemnější mřížkou SoC – viz [Mřížka SoC](#mřížka-soc-a-plný-výkon). Před optimalizacemi a se
starou hrubší mřížkou trval tentýž backtest 5,18 s / 4,13 s.)

Se srovnáním s ideálem je nejdelší jednotlivou úlohou samotný výpočet ideálu (≈ 2 s,
zpětný průchod přes celé období je ze své podstaty sekvenční), proto víc než ~4 procesy už
nepomůžou; bez něj backtest škáluje dál.

## Zveřejňování cen OTE a backtest

Výsledky denního trhu (SDAC) pro den dodávky D zveřejňuje OTE v den D−1:

| Dny dodávky | Zveřejnění finálních výsledků | V backtestu poprvé použito |
|---|---|---|
| do 30. 9. 2025 (hodinový trh) | **12:57** | 13:00 |
| od 1. 10. 2025 (15minutový trh) | **~13:05** (výpočet prodloužen ze 17 na 30 minut) | 13:15 |

Zdroje: OTE – webinář *Zavedení 15min. produktů na DT* (03/2025) a seminář 2025 (posun 12:57 →
13:05), *SDAC Fallback Manual* platný od 30. 9. 2025 (předběžné výsledky nejdříve 12:55,
finální „~13:05“ bez pevného času, při zpoždění nejpozději 14:20). Vlastní čas nastavíte
`--publish-time` (např. `--publish-time 14:20` pro nejhorší případ).

Backtest postupuje po čtvrthodinách. V čase `t` zná jen dny, jejichž ceny byly zveřejněny do
`t`; horizont tedy končí o půlnoci dnešního dne (před zveřejněním) nebo zítřejšího dne (po
něm). Spustí stejný optimalizátor jako `plan`, provede **jen první čtvrthodinu**, přenese SoC
a pokračuje. Protože rozhodnutí DP v čase `t` závisí jen na čtvrthodinách od `t` do konce
horizontu, stačí optimalizaci přepočítat jen při zveřejnění nových cen (≈ jednou denně) –
rozhodnutí jsou přitom bit po bitu stejná, jako by v každé čtvrthodině běžel `plan --at t`
(ověřuje test). Pro poslední den období se stáhnou i ceny následujícího dne, protože by v
13:05 už byly známé.

**Srovnání s ideálem** („perfect foresight“) vyřeší celé období najednou se znalostí všech
cen. Oba výsledky se porovnávají včetně změny hodnoty energie v baterii (ocenění jako
`--terminal value`); poměr ukazuje, kolik z teoretického maxima klouzavý plán získal.

## Výstupy

- **Text** – souhrn a tabulka (časy v místním čase s označením CET/CEST).
- **`--json`** – jediný JSON objekt na stdout s polem `warnings`; chyba jako
  `{"error": "…", "warnings": […]}` (návratový kód 2).
- **`-o soubor.csv`** – jedna řádka na čtvrthodinu: `start, spot_czk_mwh, buy_czk_kwh,
  sell_czk_kwh, action, power_kw, grid_kwh` (+ nákup, − prodej), `soc_start_pct, soc_end_pct,
  cash_czk, fees_czk` (podíl měsíčních poplatků na čtvrthodinu) (+ u backtestu `known_until` =
  konec cen známých v tu chvíli).
- **`-o soubor.json`** – totéž co `--json` včetně všech čtvrthodin.
- **`--breakdown-csv soubor.csv`** – řádky rozpisu (`-b`) jako CSV.

### Rozpis po hodinách a čtvrthodinách

Rozpis se vypíše **jen s volbou `-b`** (výchozí je `none`; dřív backtest vypisoval rozpis po
měsících vždy – teď je potřeba `-b month`). `backtest` umí `-b month`, `day`, `hour`
a `quarter-hour`, `plan` (pokrývá nejvýš den a půl) `hour` a `quarter-hour`:

```text
bess-ote backtest … --from 2026-10-14 -b hour
bess-ote backtest … --from 2026-10-14 -b qh --breakdown-csv ctvrthodiny.csv
bess-ote plan … -b hour --json
```

Řádek hodinového / čtvrthodinového rozpisu (JSON, CSV): `period` a `end` (místní čas s časovou
zónou), `quarters` (počet čtvrthodin v řádku), `action` (`charge` / `discharge` / `wait`;
u hodiny se smíšenými akcemi podle čistého toku energie, `mixed (…)` při nabíjení i vybíjení
v téže hodině, `no price` = čtvrthodiny bez ceny OTE), `pattern` (průběh hodiny po
čtvrthodinách: `C` nabíjení, `D` vybíjení, `-` čekání, `.` bez ceny), průměrné ceny
`spot_czk_mwh`, `buy_czk_kwh`, `sell_czk_kwh`, `power_kw` (průměrný čistý výkon), `grid_kwh`,
`soc_start_pct`, `soc_end_pct` a součty `bought_kwh`, `cost_czk`, `sold_kwh`, `revenue_czk`,
`wear_czk`, `profit_czk`, `fees_czk`, `net_profit_czk`.

- **Energie a peníze se sčítají**, ceny jsou prostý průměr čtvrthodin s cenou. Do 30. 9. 2025
  jsou ceny OTE hodinové (v Kč), takže všechny 4 čtvrthodiny hodiny mají stejnou cenu a
  průměr je přesně hodinová cena; od 1. 10. 2025 je to průměr čtyř 15minutových cen (v EUR
  přepočtených kurzem ČNB) – stejně jako hodinový index OTE.
- **Součty řádků se rovnají souhrnu** (ověřují testy; na skutečných datech červen–říjen
  2026 do 0,005 Kč – hodnoty v JSON/CSV mají 6 desetinných míst, text 3).
- **Přechod času:** hodina = čtvrthodiny jedné hodiny UTC, takže v den přechodu na zimní čas
  je 25 řádků (02:00 CEST a 02:00 CET zvlášť, 100 čtvrthodin) a na letní čas 23 řádků (92
  čtvrthodin).
- Čtvrthodiny, kdy backtest neznal ceny, zůstanou jako řádky `no price` – kvůli měsíčním
  poplatkům a souvislé řadě SoC.

Ukázka (skutečné ceny OTE, 14. 10. 2026, 10 kWh / 5 kW, parametry z rychlého startu, bez
měsíčních poplatků), výřez hodinového rozpisu:

```text
hour                  acts action                spot     buy    sell   bought     sold  SoC %    profit
(local)                                       CZK/MWh CZK/kWh CZK/kWh      kWh      kWh    end       CZK
2026-10-14 16:00 CEST ---- wait               3654.65   6.631   3.355    0.000    0.000   99.9     0.000
2026-10-14 17:00 CEST ---- wait               5499.18  10.084   5.199    0.000    0.000   99.9     0.000
2026-10-14 18:00 CEST ---D discharge          6620.78  11.441   6.321    0.000    1.250   86.8     8.862
2026-10-14 19:00 CEST DDDD discharge          7198.73  12.141   6.899    0.000    5.000   34.0    34.494
2026-10-14 20:00 CEST DD-- discharge          6576.03  11.387   6.276    0.000    2.281   10.0    15.219
2026-10-14 21:00 CEST ---- wait               5837.77  10.494   5.538    0.000    0.000   10.0     0.000
```

a čtvrthodinového rozpisu kolem večerní špičky:

```text
quarter-hour               action                spot     buy    sell   bought     sold  SoC %    profit
(local)                                       CZK/MWh CZK/kWh CZK/kWh      kWh      kWh    end       CZK
2026-10-14 18:00 CEST      wait               6088.75  10.798   5.789    0.000    0.000   99.9     0.000
2026-10-14 18:15 CEST      wait               6297.96  11.051   5.998    0.000    0.000   99.9     0.000
2026-10-14 18:30 CEST      wait               6706.64  11.545   6.407    0.000    0.000   99.9     0.000
2026-10-14 18:45 CEST      discharge          7389.79  12.372   7.090    0.000    1.250   86.8     8.862
2026-10-14 19:00 CEST      discharge          6997.68  11.898   6.698    0.000    1.250   73.6     8.372
2026-10-14 19:15 CEST      discharge          7232.95  12.182   6.933    0.000    1.250   60.4     8.666
2026-10-14 19:30 CEST      discharge          7282.15  12.242   6.982    0.000    1.250   47.2     8.728
2026-10-14 19:45 CEST      discharge          7282.15  12.242   6.982    0.000    1.250   34.0     8.728
2026-10-14 20:00 CEST      discharge          7111.17  12.035   6.811    0.000    1.250   20.9     8.514
2026-10-14 20:15 CEST      discharge          6801.38  11.660   6.501    0.000    1.031   10.0     6.705
2026-10-14 20:30 CEST      wait               6306.00  11.061   6.006    0.000    0.000   10.0     0.000
2026-10-14 20:45 CEST      wait               6085.58  10.794   5.786    0.000    0.000   10.0     0.000
```

## Konfigurační soubor

Výchozí umístění `~/.config/bess-ote/config.toml` (Windows `%APPDATA%\bess-ote\config.toml`),
jinak `--config` nebo `BESS_OTE_CONFIG`. Volby z příkazové řádky mají přednost. Vzor:
[`examples/config.example.toml`](examples/config.example.toml).

```toml
[battery]
capacity_kwh = 10
charge_kw = 5
efficiency = 90
soc_min = 10

[buy]
add_czk_mwh = 400
vat = 21
monthly_fee = 147     # Kč/měsíc bez DPH

[sell]
add_czk_mwh = -300

[distribution]
high_czk_kwh = 2.435
low_czk_kwh = 0.416
monthly_fee = 257     # Kč/měsíc bez DPH (jistič …)

[low_tariff]
hours = "1:15-5:15,12:30-16:30"
hours_weekend = "2:45-6:45,13:00-17:00"
holidays = "weekend"
```

## Data a mezipaměť

- Ceny: veřejná SOAP služba OTE (`GetDamPriceE` do 30. 9. 2025, `GetDamPricePeriodE` PT15M
  od 1. 10. 2025), kurz EUR/CZK: denní fixing ČNB (`year.txt`). Žádné přihlašování.
- Mezipaměť: `$BESS_OTE_CACHE_DIR`, jinak `%LOCALAPPDATA%\bess-ote` (Windows), jinak
  `$XDG_CACHE_HOME/bess-ote` (`~/.cache/bess-ote`, i v Termuxu). Ukládají se jen kompletní
  minulé dny (přesný počet period včetně přechodu na letní/zimní čas), dnešek a budoucí dny se
  vždy stahují znovu. `--no-cache` mezipaměť vypne, `--refresh-cache` vše stáhne znovu.
  Opakovaný backtest za stejné období tak OTE vůbec nekontaktuje.

## Omezení

- Model je **čistá arbitráž se sítí**: nezohledňuje vlastní spotřebu domácnosti ani FVE (energie
  z baterie se prodává, do baterie se nakupuje). Pro baterii za elektroměrem s vlastní spotřebou
  je skutečný přínos jiný (vybíjení do spotřeby šetří celou nákupní cenu včetně distribuce).
- Rezervovaný příkon, jistič a limity přetoků distributora se nekontrolují – nastavte výkon
  baterie odpovídajícím způsobem.
- Ceny denního trhu jsou známé jen den dopředu; vnitrodenní trh a regulační energie se
  nemodelují.
- Účinnost je konstantní (nezávislá na výkonu a SoC), samovybíjení se zanedbává.
- Zpožděné zveřejnění cen (SDAC delay, nejpozději 14:20) se v backtestu nemodeluje
  automaticky; použijte `--publish-time`.

## Vývoj

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check . && pytest
```

Testy používají jen syntetické ceny a zfalšovanou službu OTE (bez sítě).

```text
src/bess_ote/
  cli.py          příkazy plan a backtest, výstupy
  optimizer.py    DP optimalizátor (stav = energie, akce = nabíjet/vybíjet/čekat)
  planner.py      sestavení úlohy z cen známých v daném okamžiku, ocenění zbylé energie
  backtest.py     klouzavá simulace a srovnání s ideálem
  market.py       čtvrthodinová řada cen, časy zveřejnění OTE
  tariff.py       nákup, výkup, distribuce, DPH
  battery.py      parametry baterie
  ote.py, cnb.py  ceny OTE (SOAP) a kurzy ČNB s mezipamětí (převzato z egd-api-client)
  cache.py, lowtariff.py, timeutil.py, config.py, http.py, models.py
```

## Licence

[MIT](LICENSE) © 2026 OOtta
