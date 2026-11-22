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
- [Algoritmus](#algoritmus)
- [Zveřejňování cen OTE a backtest](#zveřejňování-cen-ote-a-backtest)
- [Výstupy](#výstupy)
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

# Backtest za červen–říjen 2026 se stejnými tarify, rozpis po měsících
bess-ote backtest -c 10 -p 5 --soc-min 10 --from 2026-06 --to 2026-10 \
  -a 400 --buy-vat 21 --sell-add -300 \
  --distribution-high 2.435 --distribution-low 0.416 \
  --low-tariff-hours "1:15-5:15,12:30-16:30" \
  --low-tariff-hours-weekend "2:45-6:45,13:00-17:00" --holidays weekend
```

Ukázka výstupu `plan --next`:

```text
Plan as of 2026-11-22 11:07 CET: 2026-11-22 11:15 CET – 2026-11-23 00:00 CET (51 quarter-hours)
NEXT 11:15–11:30  WAIT, SoC 50.0 % → 50.0 %
```

Ukázka výstupu `backtest` (skutečné ceny OTE, parametry viz poslední příklad výše):

```text
Backtest 2026-06-01 – 2026-10-31 (153 days)
Battery:      10 kWh, 5 kW, round trip 90.0 % (charge 94.9 %, discharge 94.9 %), SoC 10–100 %
Buy:          spot + 400 CZK/MWh, VAT 21 %
Sell:         spot − 300 CZK/MWh, excl. VAT
Distribution: VT 2.435 / NT 0.416 CZK/kWh (NT 01:15–05:15, 12:30–16:30; Sat/Sun 02:45–06:45, 13:00–17:00; holidays as weekend) (on purchase only)
Prices:       prices for day D known on D−1 at 12:57 (delivery ≤ 2025-09-30) / 13:05 (from 2025-10-01), used from the next quarter-hour; 154 re-optimisations

Bought             1314.45 kWh   cost          3195.02 CZK
Sold               1183.01 kWh   revenue       6514.56 CZK
PROFIT                                   3319.54 CZK (21.70 CZK/day)
Cycles               138.6 (equivalent full cycles of usable capacity)
SoC           10.0 % → 10.0 % (stored energy change worth 0.00 CZK; profit incl. it 3319.54 CZK)
Perfect foresight (all prices known in advance): profit 3327.94 CZK incl. stored energy, 138.7 cycles; rolling plan captures 99.7 %
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
| `--soc-step KWH` | jemnost mřížky SoC optimalizátoru | kapacita / 400 |

**Nákup (spot × násobek + přirážka)**

| Volba | Význam | Výchozí |
|---|---|---|
| `-m`, `--buy-multiplier X` | násobek spotové ceny | 1 |
| `-a`, `--buy-add KČ_MWH` | přirážka dodavatele | 0 |
| `--buy-negative-multiplier X`, `--buy-negative-add KČ_MWH` | jiný vzorec pro záporné spotové ceny | = normální |
| `--buy-negative as-is\|clamp-spot\|clamp-total` | zacházení se zápornými cenami | `as-is` |
| `--buy-vat PCT` | DPH na nákup (energie + distribuce + poplatky) | 0 |

**Výkup (spot × násobek + přirážka)**

| Volba | Význam | Výchozí |
|---|---|---|
| `--sell-multiplier X` | násobek | 1 |
| `--sell-add KČ_MWH` | přirážka, obvykle záporná = poplatek výkupce (např. `-300`) | 0 |
| `--sell-negative-multiplier`, `--sell-negative-add`, `--sell-negative` | jako u nákupu | = normální |
| `--sell-vat PCT` | DPH přičtená k tržbě (jen plátci DPH) | 0 |

**Distribuce a poplatky (jen na nakoupenou energii)**

| Volba | Význam | Výchozí |
|---|---|---|
| `--distribution-high KČ_KWH` | distribuce ve VT (nebo jediná sazba), bez DPH | 0 |
| `--distribution-low KČ_KWH` | distribuce v NT, bez DPH (vyžaduje časy NT) | – |
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

**`backtest`**

| Volba | Význam |
|---|---|
| `--from DATUM`, `--to DATUM` | období (`RRRR-MM-DD` nebo `RRRR-MM` = celý měsíc), nejpozději do včerejška |
| `-b month\|day\|none` | rozpis (výchozí po měsících) |
| `--no-foresight` | nepočítat srovnání s dokonalou znalostí budoucnosti |
| `-o FILE` | všechny čtvrthodiny do `.csv`, nebo vše (souhrn, rozpis, čtvrthodiny) do `.json` |

## Jak se počítá cena

Pro každou čtvrthodinu se spotovou cenou `spot` (Kč/MWh):

```text
nákup  [Kč/kWh] = ( (spot × m + a) / 1000 + distribuce(VT/NT) + poplatky ) × (1 + DPH nákup)
výkup  [Kč/kWh] = ( spot × m_v + a_v ) / 1000 × (1 + DPH výkup)
```

- Pro **záporný spot** (< 0) se použijí koeficienty `--…-negative-multiplier` / `--…-negative-add`
  (co není zadáno, převezme normální hodnotu). `clamp-spot` nahradí záporný spot nulou před
  výpočtem, `clamp-total` ořízne výsledek na 0. Stejně jako v
  [egd-api-client](https://github.com/OOtta/egd-api-client).
- Distribuce se podle časů NT počítá po čtvrthodinách v místním čase (správně i ve dnech
  přechodu na letní/zimní čas); čtvrthodina, která NT protíná jen částí, dostane vážený průměr.
- **Měsíční paušály** (jistič, poplatek dodavateli) arbitráž neovlivní, proto se nezadávají.
- Ceny OTE: do 30. 9. 2025 hodinové ceny v Kč (každá platí pro 4 čtvrthodiny), od 1. 10. 2025
  čtvrthodinové ceny v EUR přepočtené kurzem ČNB platným pro den dodávky.

## Algoritmus

Optimalizace je **dynamické programování** přes jemnou mřížku uložené energie (výchozí krok
kapacita/400, tj. 0,025 kWh u 10 kWh baterie):

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
milisekund. Jediná daň za mřížku: maximální výkon se zaokrouhlí dolů na celý krok mřížky
(u 5 kW baterie např. 4,95 kW místo 5 kW, tj. < 1 %); jemnější krok nastavíte `--soc-step`.

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
  cash_czk` (+ u backtestu `known_until` = konec cen známých v tu chvíli).
- **`-o soubor.json`** – totéž co `--json` včetně všech čtvrthodin.

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

[sell]
add_czk_mwh = -300

[distribution]
high_czk_kwh = 2.435
low_czk_kwh = 0.416

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
