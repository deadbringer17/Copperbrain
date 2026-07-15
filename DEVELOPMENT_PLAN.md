# KiCad Schematic MCP — Piano di sviluppo MVP

Repository: [deadbringer17/Copperbrain](https://github.com/deadbringer17/Copperbrain)

Nome di progetto: **Copperbrain**.

## 1. Obiettivo

Realizzare un server MCP locale che permetta a un agente AI di:

1. aprire e analizzare un progetto KiCad 10;
2. comprendere componenti, pin, reti, alimentazioni e principali rischi dello schematico;
3. tradurre una richiesta in requisiti elettrici e commerciali strutturati;
4. cercare e confrontare componenti JLCPCB/LCSC per specifiche, disponibilita, tipo Basic/Extended e prezzo;
5. importare simbolo, footprint, modello 3D e datasheet del componente scelto;
6. proporre e applicare una modifica sicura allo schematico;
7. validare il risultato con ERC e controlli semantici;
8. generare BOM e stima del costo componenti per piu quantita.

Il percorso principale della demo deve funzionare interamente da una richiesta naturale a un progetto KiCad verificato, mantenendo sempre una conferma umana prima delle modifiche.

## 2. Scenario demo di riferimento

Richiesta:

> Nel mio progetto alimentato a 12 V aggiungi una sezione a 5 V / 2 A. Preferisci componenti JLCPCB Basic disponibili, package assemblabile economicamente e costo BOM basso per 10 e 100 schede.

Risultato atteso:

- analisi delle alimentazioni e dei connettori esistenti;
- requisiti normalizzati e vincoli mancanti evidenziati;
- confronto di massimo cinque componenti candidati;
- scelta esplicita dell'utente;
- importazione degli asset KiCad;
- patch dello schematico con proprieta LCSC, MPN, produttore, datasheet e footprint;
- ERC prima/dopo;
- BOM con prezzi a 10 e 100 unita e timestamp dello stock;
- snapshot ripristinabile del progetto.

## 3. Scope MVP

### Incluso

- KiCad 10.x su Windows.
- Progetti `.kicad_pro` con schematici `.kicad_sch`.
- Analisi read-only di componenti, proprieta, pin, reti e gerarchie di base.
- Ricerca JLCPCB globale tramite i moduli di JLCImport gia installati.
- Filtri per Basic/Extended, stock minimo, package, produttore e prezzo.
- Importazione di simboli, footprint, modelli 3D e datasheet.
- Inserimento e sostituzione controllata di componenti.
- Aggiunta di fili, label e simboli di alimentazione per modifiche circoscritte.
- Snapshot, anteprima, conferma, applicazione e rollback.
- ERC KiCad in JSON e confronto prima/dopo.
- BOM e stima del solo costo componenti.
- Trasporto MCP locale via stdio.

### Fuori scope iniziale

- Autorouting del PCB.
- Progettazione autonoma di circuiti arbitrariamente complessi.
- Modifica live dello schematico mentre e aperto e non salvato in KiCad.
- Preventivo completo di PCB, stencil, assemblaggio, spedizione e dazi.
- Ordini automatici o acquisti.
- Supporto multi-vendor completo.
- Server MCP esposto pubblicamente via rete.

## 4. Architettura

```text
MCP client / Codex
        |
        v
FastMCP tool layer
        |
        v
Application services
  |          |             |              |
  v          v             v              v
Project   Schematic     Component       Validation
service   service       sourcing        service
             |             |              |
             v             v              v
       kicad-sch-api   JLC adapter     kicad-cli
                          |
              +-----------+-----------+
              |                       |
              v                       v
         JLCImport                JLCPCB Tools DB

Mutazioni -> workspace temporaneo -> validazione -> conferma -> commit atomico
```

### Scelte tecnologiche

- Python 3.11+.
- SDK MCP Python ufficiale, ramo stabile v1 durante l'hackathon.
- Pydantic per contratti e validazione dei payload.
- `kicad-sch-api` come adapter per `.kicad_sch`, isolato dietro un'interfaccia interna.
- `kicad-cli.exe` invocato con percorso rilevato automaticamente.
- JLCImport usato come dipendenza/adattatore, senza automazione GUI.
- JLCPCB Tools usato per database, fasce prezzo e generazione BOM/CPL dove utile.
- SQLite per cache locale di ricerche e snapshot dei prezzi.
- Pytest per unit, integration e golden-file tests.

## 5. Principi di sicurezza e affidabilita

1. L'LLM non scrive mai direttamente file KiCad.
2. Ogni tool accetta input tipizzati e applica una allowlist di operazioni.
3. Le mutazioni lavorano prima su una copia temporanea del progetto.
4. Prima dell'applicazione vengono prodotti diff semantico ed ERC.
5. L'applicazione richiede un `change_set_id` gia preparato e confermato.
6. Prima del commit viene verificato che i file originali non siano cambiati tramite hash.
7. Ogni commit crea uno snapshot ripristinabile.
8. Nessun comando di shell arbitrario viene esposto tramite MCP.
9. Download consentiti solo da host configurati e con timeout/limiti di dimensione.
10. Prezzi, disponibilita e datasheet esterni sono sempre marcati con fonte e timestamp.
11. PDF, BOM, report e copie di anteprima destinati all'utente sono salvati esclusivamente in `copperbrain-output/` nella cartella del progetto aperto; workspace, cache e snapshot restano nello storage privato di Copperbrain.

## 6. Contratto degli strumenti MCP

### Fase A — progetto e analisi

| Tool | Tipo | Risultato essenziale |
|---|---|---|
| `detect_kicad` | lettura | versioni, percorsi CLI, librerie e plugin rilevati |
| `open_project` | lettura | sessione progetto, file, hash e versione KiCad |
| `get_project_summary` | lettura | fogli, componenti, reti, alimentazioni e stato |
| `analyze_schematic` | lettura | grafo elettrico, warning e osservazioni motivate |
| `trace_net` | lettura | pin e componenti collegati a una rete |
| `run_erc` | lettura | violazioni KiCad normalizzate |

### Fase B — sourcing

| Tool | Tipo | Risultato essenziale |
|---|---|---|
| `search_components` | rete/lettura | candidati normalizzati e filtrati |
| `get_component_details` | rete/lettura | specifiche, price breaks, stock, datasheet e asset |
| `compare_components` | lettura | matrice requisiti/candidati con motivazioni |
| `find_alternatives` | rete/lettura | sostituti compatibili e differenze |
| `estimate_component_cost` | lettura | costo per quantita e assunzioni |

### Fase C — importazione e modifica

| Tool | Tipo | Risultato essenziale |
|---|---|---|
| `import_component_assets` | scrittura controllata | libreria simboli, footprint, 3D, datasheet |
| `prepare_schematic_change` | anteprima | `change_set_id`, operazioni, diff e rischi |
| `validate_change` | lettura | parsing, riferimenti, pin, reti ed ERC temporaneo |
| `apply_change` | scrittura confermata | snapshot e nuovi hash |
| `rollback_change` | scrittura confermata | ripristino snapshot |

### Fase D — BOM

| Tool | Tipo | Risultato essenziale |
|---|---|---|
| `generate_bom` | lettura/output | BOM normalizzata con LCSC/MPN |
| `estimate_bom_cost` | lettura | costo per 1/10/100 unita, stock e mancanti |
| `suggest_bom_substitutions` | rete/lettura | alternative economiche o disponibili |

### Estensione PCB, dopo il core schematico

| Tool | Tipo | Risultato essenziale |
|---|---|---|
| `export_netlist` | lettura/output | netlist KiCad validata |
| `update_pcb_from_netlist` | scrittura controllata | footprint aggiunti/aggiornati |
| `run_drc` | lettura | violazioni PCB prima/dopo |

## 7. Modello dati minimo

```text
ProjectSession
  id, root, project_file, schematic_files, pcb_file, hashes, kicad_version

RequirementSet
  functional, electrical, mechanical, commercial, sourcing, assumptions

ComponentCandidate
  lcsc, mpn, manufacturer, description, package, basic_extended,
  stock, price_breaks, datasheet_url, asset_availability, score, evidence

ChangeSet
  id, project_hash, operations, affected_files, semantic_diff,
  validation_report, status, snapshot_id

BomLine
  references, quantity_per_board, value, footprint, lcsc, mpn,
  unit_prices, stock, extended_cost, price_timestamp
```

## 8. Piano per milestone

### M0 — Fondazioni e contratti (0,5 giorno)

- creare repository Python e struttura pacchetto;
- bloccare dipendenze e versione SDK MCP;
- definire modelli Pydantic e formato errori;
- aggiungere configurazione dei percorsi locali;
- creare fixture KiCad minima.

**Done quando:** il server parte via stdio, espone `detect_kicad` e passa smoke test.

### M1 — Lettura e analisi schematico (1 giorno)

- rilevare/aprire un progetto;
- caricare schematici senza modificarli;
- estrarre componenti, proprieta, pin, label e reti;
- costruire un grafo elettrico normalizzato;
- esporre summary, trace net e analisi iniziale;
- integrare ERC JSON tramite CLI.

**Done quando:** un progetto demo produce summary deterministica ed ERC ripetibile.

### M2 — Ricerca e ranking JLCPCB (1 giorno)

- creare adapter JLCImport;
- normalizzare risultati e price breaks;
- filtri Basic/Extended, stock e package;
- scoring deterministico contro `RequirementSet`;
- cache con timestamp;
- confronto massimo cinque candidati.

**Done quando:** una richiesta nota restituisce candidati spiegabili e costo per quantita.

### M3 — Importazione asset (0,75 giorno)

- scaricare/importare simbolo, footprint e modello 3D;
- salvare datasheet;
- aggiornare `sym-lib-table` e `fp-lib-table` del progetto;
- validare esistenza asset e corrispondenza pin/pad di base;
- rendere l'operazione idempotente.

**Done quando:** il componente importato e visibile nelle librerie del progetto KiCad.

### M4 — Patch sicura dello schematico (1,5 giorni)

- implementare operazioni add/replace/update/connect/label;
- snapshot e workspace temporaneo;
- diff semantico;
- rilevamento conflitti tramite hash;
- validazione post-scrittura;
- apply e rollback atomici.

**Done quando:** il componente puo essere aggiunto o sostituito senza corrompere il progetto e con rollback verificato.

### M5 — BOM e costo (0,75 giorno)

- estrarre BOM normalizzata;
- unire metadata JLCPCB;
- calcolare costi a 1/10/100 schede;
- distinguere Basic/Extended;
- segnalare prezzi mancanti, MOQ e stock insufficiente;
- esportare JSON, CSV e report Markdown.

**Done quando:** i totali sono riproducibili da fixture e riportano assunzioni/timestamp.

### M6 — Integrazione e demo (1,5 giorni)

- test end-to-end dello scenario di riferimento;
- gestione degli errori e messaggi orientati all'utente;
- documentazione installazione e configurazione MCP;
- progetto demo riproducibile;
- script della demo e registrazione video sotto tre minuti;
- sessione Codex principale tracciabile per `/feedback`.

**Done quando:** una nuova installazione puo ripetere la demo seguendo il README.

## 9. Strategia di test

### Unit test

- normalizzazione di requisiti e candidati;
- scoring e ordinamento;
- price break e calcolo BOM;
- validazione degli input MCP;
- diff semantico e conflitti hash.

### Golden-file test

- schematico originale e output atteso;
- aggiunta componente;
- sostituzione componente;
- aggiunta label/fili;
- rollback byte-per-byte.

### Integration test

- JLCImport con risposta registrata e test live opzionale;
- importazione asset in directory temporanea;
- `kicad-cli sch erc --format json`;
- apertura del file risultante con parser KiCad-compatible.

### End-to-end

- progetto demo copiato in una directory temporanea;
- richiesta -> ricerca -> scelta -> import -> patch -> ERC -> BOM;
- nessuna modifica ai file originali fino ad `apply_change`;
- rollback completo verificato.

## 10. Criteri di accettazione MVP

- [ ] Il server viene rilevato da un client MCP locale.
- [ ] KiCad 10.x e i due plugin JLC vengono individuati automaticamente.
- [ ] Un progetto esistente viene analizzato senza modifiche.
- [ ] ERC viene eseguito e restituito in forma strutturata.
- [ ] La ricerca restituisce componenti con prezzo, stock e categoria.
- [ ] La scelta e motivata rispetto ai requisiti.
- [ ] Simbolo, footprint e 3D vengono importati nel progetto.
- [ ] Una modifica circoscritta dello schematico viene preparata e mostrata in anteprima.
- [ ] Nessuna mutazione avviene senza `change_set_id` valido.
- [ ] Apply e rollback sono entrambi verificati.
- [ ] La BOM contiene LCSC/MPN e costi per almeno due quantita.
- [ ] Il report distingue costo componenti da costi PCB/assembly/spedizione.
- [ ] Il percorso demo termina senza correzioni manuali dei file.

## 11. Rischi e mitigazioni

| Rischio | Impatto | Mitigazione |
|---|---:|---|
| Schematic editor aperto sovrascrive modifiche esterne | alto | richiedere file salvato/editor chiuso nell'MVP; hash prima del commit |
| API JLCPCB non ufficiale cambia | alto | adapter isolato, timeout, cache e fixture registrate |
| Dati EasyEDA errati | alto | confronto pin/pad, datasheet e conferma utente |
| `kicad-sch-api` non copre un costrutto KiCad 10 | alto | fixture reali, copia temporanea e fallback read-only |
| LLM propone circuito scorretto | alto | regole deterministiche, evidence, ERC e approvazione umana |
| Scope troppo ampio | alto | demo limitata ad add/replace e circuito applicativo circoscritto |
| Prezzo non coincide col preventivo finale | medio | timestamp e separazione esplicita dei costi non inclusi |
| Differenze tra KiCad 10.0.1 e 10.0.4 | medio | test su 10.0.1, aggiornamento e test finale su 10.0.4 |

## 12. Ordine operativo immediato

1. Creare lo scaffold Python e un progetto KiCad demo minimale.
2. Implementare `detect_kicad`, `open_project`, `get_project_summary` e `run_erc`.
3. Congelare un test di ricerca JLCPCB e definire `ComponentCandidate`.
4. Dimostrare l'importazione di un solo componente in una directory temporanea.
5. Implementare `prepare_schematic_change` per una sostituzione semplice.
6. Chiudere il primo vertical slice con apply, ERC e rollback.
7. Solo dopo aggiungere generazione di un piccolo sottocircuito e sincronizzazione PCB.

## 13. Estensione approvata — regole di progetto PCB via MCP

Questa estensione post-MVP consente di analizzare, proporre, preparare e applicare netclass e
custom design rules KiCad 10 senza introdurre autorouting o generazione autonoma del layout.

### Contratti MCP

| Tool | Tipo | Risultato essenziale |
|---|---|---|
| `analyze_pcb_constraints` | lettura | netclass esistenti e classificazione motivata delle reti |
| `propose_design_rules` | lettura | regole tipizzate e deterministiche da profilo produttivo e intenti elettrici |
| `prepare_pcb_rule_change` | anteprima | `.kicad_pro`/`.kicad_dru` temporanei, diff, rischi e DRC |
| `validate_pcb_rule_change` | lettura | validazione strutturale e nuovo DRC temporaneo |
| `apply_pcb_rule_change` | scrittura confermata | snapshot e sostituzione atomica dopo hash/editor check |
| `rollback_pcb_rule_change` | scrittura confermata | ripristino byte-per-byte, inclusa rimozione di un `.kicad_dru` nuovo |
| `run_drc` | lettura | violazioni PCB KiCad normalizzate |

### Regole di prodotto e sicurezza

- L'MCP accetta esclusivamente `ManufacturingProfile`, `NetRuleRequirement` e `PcbRuleSet`
  tipizzati; non accetta testo libero `.kicad_dru` o espressioni di condizione arbitrarie.
- Un adapter allowlisted rende netclass e constraint KiCad, preservando le regole custom non
  gestite da Copperbrain.
- La classificazione automatica usa soltanto nomi delle reti e connettivita e dichiara sempre le
  proprie assunzioni. Non deduce corrente, tensione o impedenza dai componenti.
- La larghezza di una netclass e confrontata con geometria, pitch e dimensione minima dei pad di
  ogni footprint collegato. Quando la larghezza preferita non entra in sicurezza nel package,
  Copperbrain genera un neck-down locale limitato al courtyard del componente.
- Il limite di fanout predefinito e l'80% della dimensione minima del pad e non puo scendere sotto
  la larghezza minima del produttore. Un conflitto fra package e capacita produttive causa un
  rifiuto strutturato, non una pista non fabbricabile.
- Se un footprint locale non ha courtyard e il PCB non contiene ancora footprint, Copperbrain ne
  prepara uno rettangolare validato da KiCad nello stesso change set. Su un PCB gia popolato il
  sistema rifiuta la modifica finche il footprint non viene aggiornato in modo controllato.
- Le reti `high_current` richiedono corrente o larghezza esplicita. Le reti `high_voltage`
  richiedono clearance esplicitamente revisionata. Le geometrie differenziali senza stackup
  verificato sono marcate come non controllate in impedenza.
- Il dimensionamento da corrente e una stima deterministica e conservativa basata su rame,
  layer e incremento termico; non costituisce certificazione normativa.
- Il workflow resta `prepare -> preview -> explicit confirmation -> validate -> apply`, con
  workspace privato, DRC, hash anti-stale, snapshot, apply atomico e rollback.

### Criteri di accettazione

- [x] Le regole sono producibili da un client MCP senza passare sintassi KiCad libera.
- [x] Netclass, assegnazioni, clearance, creepage, larghezze, via, lunghezze e geometrie
  differenziali sono tipizzate.
- [x] Le larghezze sono verificate contro pad/pitch e le reti larghe ricevono neck-down locali.
- [x] Courtyard generati e footprint modificati sono inclusi in preview, hash, snapshot e rollback.
- [x] Il sorgente non cambia durante proposta, preparazione o validazione.
- [x] Le regole generate sono accettate dal DRC KiCad 10.0.1.
- [x] Apply richiede conferma esplicita, editor chiuso e hash non stale.
- [x] Rollback ripristina byte-per-byte `.kicad_pro` e lo stato originario del `.kicad_dru`.
- [x] Autorouting e modifica delle piste restano fuori scope.
