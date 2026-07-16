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

## 14. Estensione approvata — ispezione e placement PCB via MCP

Questa estensione introduce un vertical slice completo per ispezionare la geometria del board,
analizzare la qualita del placement, proporre posizioni deterministiche e applicare spostamenti di
footprint in sicurezza. Routing, zone in rame, keepout e modifica del board outline restano fuori
da questa estensione.

### Contratti MCP

| Tool | Tipo | Risultato essenziale |
|---|---|---|
| `get_pcb_summary` | lettura | outline, footprint, net, piste, via, zone e stato IPC |
| `inspect_pcb_net` | lettura | pad, riferimenti, lunghezza instradata, via e layer di una rete |
| `get_footprint_placement` | lettura | posizione, rotazione, layer, lock e bounding box |
| `analyze_placement` | lettura | score, overlap e footprint esterni a Edge.Cuts |
| `propose_component_placement` | lettura | operazioni tipizzate e deterministiche in una regione validata |
| `prepare_placement_change` | anteprima | copia temporanea, diff semantico, PDF, DRC e rischi |
| `validate_placement_change` | lettura | parsing strutturale e DRC senza nuove regressioni |
| `apply_placement_change` | scrittura confermata | snapshot e sostituzione atomica dopo hash/editor check |
| `rollback_placement_change` | scrittura confermata | ripristino byte-per-byte del PCB |
| `export_pcb_preview` | lettura/output | copia di anteprima e PDF sotto `copperbrain-output/previews/` |

### Architettura e sicurezza

- `PcbDesignService` orchestra query, proposta e change set senza dipendere dal trasporto MCP.
- `PcbFileAdapter` analizza il formato PCB e applica esclusivamente `PlacementOperation` tipizzate;
  non accetta S-expression o frammenti KiCad forniti dal chiamante.
- `KiCadPcbIpcAdapter` usa il binding ufficiale `kicad-python` per board gia aperti e verifica che il
  documento IPC corrisponda esattamente al percorso atteso prima di iniziare una transazione.
- L'IPC e opzionale a runtime. Ispezione, proposta, preview e validazione restano disponibili in
  modalita offline tramite file adapter e `kicad-cli`.
- La proposta usa geometria courtyard incorporata quando disponibile, altrimenti limiti
  conservativi derivati da pad e grafica del footprint. Non deduce intento meccanico, termico, RF,
  SI o PI.
- La mutazione segue sempre `prepare -> preview -> explicit confirmation -> validate -> apply`, con
  workspace privato, PDF progetto, DRC comparativo, hash anti-stale, snapshot e rollback.
- Apply e rollback richiedono editor salvato e chiuso. Il progetto live non cambia durante analisi,
  proposta, preparazione o validazione.
- Spostamento e rotazione sul lato corrente restano disponibili tramite file adapter. I cambi lato
  `F.Cu`/`B.Cu` passano esclusivamente da un worker a comando fisso basato sull'API `pcbnew` di
  KiCad, che trasforma in modo coordinato pad, grafica, testi e modelli 3D nella copia temporanea.
- La proposta compatta usa connettivita e coordinate pad, valuta rotazioni ortogonali e inviluppo,
  mantiene i connettori verso il bordo e riserva corridoi maggiori alle reti di potenza inferite.
  Il bottom automatico e limitato ai piccoli passivi SMD; THT, IC e potenza conservano il lato.

### Criteri di accettazione

- [x] Outline, footprint e connettivita PCB sono restituiti in modelli tipizzati.
- [x] L'analisi segnala overlap e footprint esterni ai limiti Edge.Cuts.
- [x] Un board vuoto o senza outline restituisce `empty_board`/`missing_outline` e non riceve uno
  score positivo.
- [x] La proposta e deterministica, collision-free e limitata a riferimenti e regioni esplicite.
- [x] Footprint mancanti o bloccati causano un rifiuto strutturato.
- [x] La preview contiene copia del progetto e PDF nella directory di output consentita.
- [x] Il DRC temporaneo non introduce nuovi errori rispetto al board originale.
- [x] Apply richiede conferma, editor chiuso e hash sorgente non stale.
- [x] Rollback ripristina il file `.kicad_pcb` byte-per-byte.
- [x] Il binding IPC ufficiale e rilevato dinamicamente e non e requisito per i test offline.
- [x] Routing, zone, keepout e outline mutation restano fuori scope.
- [x] Il cambio lato usa l'API KiCad nella preview, resta tipizzato ed e coperto da DRC e conferma.

## 15. Estensione approvata — inizializzazione headless PCB via MCP

Questa estensione prepara via codice un PCB iniziale non instradato partendo dallo schematico e da
un piano geometrico tipizzato. E distinta dal placement su board esistente della sezione 14 ed e
limitata a PCB vuoti, senza footprint, piste, zone o `Edge.Cuts` preesistenti.

### Contratti MCP

| Tool | Tipo | Risultato essenziale |
|---|---|---|
| `prepare_pcb_layout_change` | anteprima | sincronizzazione footprint, contorno rettangolare, placement completo, fori M3, PDF, ERC e DRC in workspace |
| `validate_pcb_layout_change` | lettura | nuova validazione parser/ERC/DRC/placement della copia preparata |
| `apply_pcb_layout_change` | scrittura confermata | snapshot e sostituzione atomica di schematico, PCB e regole gestite |
| `rollback_pcb_layout_change` | scrittura confermata | ripristino byte-per-byte dei file applicati |

### Architettura, sicurezza e limiti

- L'MCP accetta solo `PcbLayoutPlan`, `RectangularBoardOutline`, `PlacementOperation` e
  `MountingHoleSpec`; nessun frammento S-expression e accettato al confine pubblico.
- `PcbLayoutService` orchestra workspace, sincronizzazione dello schematico, netlist, composizione,
  upgrade KiCad, preview, confronto ERC/DRC, hash, snapshot, apply e rollback.
- `PcbLayoutAdapter` risolve footprint installati o di progetto e compone il board esclusivamente
  dalle operazioni tipizzate. Ogni componente dello schematico deve comparire una sola volta.
- I placement iniziali possono specificare `F.Cu` o `B.Cu`; il bottom viene realizzato dal worker
  KiCad a comando fisso dopo la composizione tipizzata, prima di parser, PDF e DRC.
- Le regole Copperbrain di clearance e creepage tra oggetti sono rese con esclusione dello stesso
  parent footprint; le vecchie regole gestite sono migrate soltanto nella copia temporanea.
- Il sorgente non cambia durante prepare e validate. Apply richiede change set validato, conferma
  esplicita, editor chiuso, hash non stale e snapshot ripristinabile.
- Routing, autorouting, zone rame, keepout, stackup, fabbricazione e verifica termica/EMC/SI/PI
  restano fuori scope. Il risultato e uno scheletro PCB revisionabile, non un progetto pronto alla
  produzione.

### Criteri di accettazione

- [x] Un client MCP puo costruire una preview PCB completa senza automazione GUI.
- [x] Contorno, placement, fori e override footprint sono tipizzati e deterministici.
- [x] Il piano incompleto, i riferimenti duplicati e i board non vuoti sono rifiutati.
- [x] La preview e pubblicata soltanto sotto `copperbrain-output/previews/<change-set-id>/`.
- [x] ERC, DRC e placement devono passare senza nuove regressioni prima dell'apply.
- [x] Apply e rollback mantengono conferma, editor-state, stale-hash e atomicita.
- [x] Nessuna pista, zona o keepout viene generato implicitamente.

## 16. Estensione approvata — routing PCB controllato via MCP

Questa estensione, approvata il 15 luglio 2026, abilita il completamento deterministico delle
connessioni PCB tramite segmenti e via tipizzati. Supera l'esclusione generale del routing solo
per questo workflow controllato; zone, keepout, tuning d'impedenza e certificazioni SI/PI/EMC
restano fuori scope.

### Contratti MCP

| Tool | Tipo | Risultato essenziale |
|---|---|---|
| `get_routing_backend_status` | lettura | disponibilita locale di Java, FreeRouting e bridge Python KiCad |
| `analyze_unrouted_nets` | lettura | gruppi di pad elettricamente disconnessi per rete |
| `propose_pcb_routing` | lettura | candidati FreeRouting tipizzati, valutati e ordinati deterministicamente |
| `prepare_routing_change` | anteprima | PCB temporaneo, diff, PDF, connettivita e DRC comparativo |
| `validate_routing_change` | lettura | nuova verifica parser, connessioni richieste e DRC |
| `apply_routing_change` | scrittura confermata | snapshot e sostituzione atomica dopo hash/editor check |
| `rollback_routing_change` | scrittura confermata | ripristino byte-per-byte del PCB |
| `restore_routing_snapshot` | scrittura confermata | recupero atomico di uno snapshot privato dopo verifica dell'identita della board |

### Architettura, sicurezza e limiti

- Il confine pubblico accetta soltanto `RoutingRequest`, `RoutingPlan`, `RouteSegment` e
  `RouteVia`; non accetta S-expression o condizioni KiCad libere.
- `PcbRoutingService` orchestra un backend specializzato locale e non implementa il pathfinding.
  `FreeRoutingAdapter` usa soltanto comandi fissi, Java locale e il JAR configurato; nessun comando
  o argomento eseguibile viene accettato dal confine MCP.
- Il round-trip usa `pcbnew.ExportSpecctraDSN` e `pcbnew.ImportSpecctraSES` nel Python distribuito
  con KiCad, sempre in workspace privato. Il risultato esterno viene ridotto a delta tipizzati
  `RouteSegment`/`RouteVia`; la rimozione o modifica di rame preesistente e rifiutata.
- Copperbrain puo eseguire configurazioni `prioritized` e `sequential`, misura completezza,
  regressioni DRC, lunghezza, segmenti e via, quindi sceglie il candidato con ranking
  deterministico. L'AI puo spiegare e valutare le evidenze, ma non puo superare i gate rigidi.
- FreeRouting e opzionale e rilevato dinamicamente. La sua assenza produce un errore strutturato
  con istruzioni di configurazione, senza fallback implicito al precedente A* lento.
- La proposta dell'autorouter non dichiara da sola la fabbricabilita. Il change set diventa
  applicabile solo se il parser accetta il PCB, le reti richieste risultano complete e KiCad DRC
  non introduce nuovi errori.
- Il workflow resta `prepare -> preview -> explicit confirmation -> validate -> apply`, con
  workspace privato, hash anti-stale, editor chiuso, snapshot, sostituzione atomica e rollback.
- Il routing non certifica impedenza, lunghezze accoppiate, ritorni di corrente, termica, SI, PI,
  EMC o conformita normativa. Questi intenti richiedono regole esplicite e revisione tecnica.

### Criteri di accettazione

- [x] Analisi e proposta sono deterministiche e limitabili a nomi rete esatti.
- [x] Segmenti e via sono tipizzati e riferiscono soltanto reti esistenti.
- [x] Dimensioni predefinite possono essere sostituite dalle netclass del progetto.
- [x] Prepare e validate non modificano il sorgente e pubblicano la preview nel progetto.
- [x] Completezza elettrica e DRC comparativo sono gate dell'apply.
- [x] Apply richiede conferma, editor chiuso e hash non stale.
- [x] Rollback ripristina il `.kicad_pcb` byte-per-byte.
- [x] Java, JAR FreeRouting e Python KiCad sono rilevati senza path macchina hard-coded.
- [x] Il bridge DSN/SES opera headless in workspace privato e non modifica il progetto sorgente.
- [x] Il delta importato e tipizzato e rifiuta rimozioni di rame o layer non supportati.
- [x] Due configurazioni candidate possono essere valutate con ranking riproducibile e strutturato.
- [x] L'assenza del backend restituisce un errore azionabile e non avvia il router A* interno.

## 17. Hardening approvato — finalizzazione PCB persistente e readiness

Questo hardening, approvato il 15 luglio 2026, rende il workflow di routing riprendibile dopo un
riavvio MCP e introduce una superficie compatta di finalizzazione. Non elimina i gate di conferma
e non estende il routing a zone, keepout o certificazioni di produzione.

### Contratti MCP

| Tool | Tipo | Risultato essenziale |
|---|---|---|
| `get_routing_change_summary` | lettura | evidenza compatta di stato, completezza, DRC, segmenti/via e preview |
| `assess_pcb_readiness` | lettura | gate elettrici e limiti produttivi non valutati tenuti separati |
| `prepare_pcb_finalization` | anteprima | proposta, workspace, preview e manifest routing persistente |
| `validate_pcb_finalization` | lettura | nuova validazione di un workflow ripreso dallo storage privato |
| `apply_pcb_finalization` | scrittura confermata | apply atomico del routing e report readiness aggiornato |
| `get_pcb_finalization_report` | lettura | report compatto corrente di un workflow persistito |

### Decisioni e criteri di accettazione

- [x] I manifest routing sono Pydantic, versionati, scritti atomicamente e confinati sotto
  `COPPERBRAIN_DATA_DIR`; workspace e snapshot fuori dalle directory private sono rifiutati.
- [x] Validate, apply e rollback possono riprendere un change set dopo il riavvio del server,
  riaprendo il progetto sorgente e ricostruendo solo path relativi validati.
- [x] L'input incrementale con rame preesistente e rifiutato per default; il policy `preserve`
  richiede una scelta esplicita ed e protetto dal watchdog.
- [x] Il processo Java ha limite wall-time, rilevamento stall e rilevamento del loop noto
  `PolylineTrace.normalize`, con cleanup del process tree e errore strutturato.
- [x] Un progetto sotto `copperbrain-output/` non puo essere aperto come sorgente o generare una
  preview ricorsiva.
- [x] Un routing elettricamente completo e pulito non viene dichiarato automaticamente pronto
  per produzione: termica, SI/PI, EMC, stackup, impedenza e DFM restano `not_assessed`.
- [x] Apply e rollback continuano a richiedere conferma esplicita, editor chiuso, hash non stale,
  snapshot e sostituzione atomica.

## 18. Estensione approvata — creazione sicura di un progetto vuoto

Questa estensione, approvata dalla richiesta benchmark del 15 luglio 2026, consente di avviare un
nuovo progetto senza copiare fixture o scrivere manualmente S-expression KiCad.

### Contratti MCP

| Tool | Tipo | Risultato essenziale |
|---|---|---|
| `prepare_project_creation` | anteprima | scaffold privato, validazione e copia sotto il futuro `copperbrain-output/previews/` |
| `validate_project_creation` | lettura | nuova verifica di progetto, schematico, PCB, ERC e DRC disponibili |
| `apply_project_creation` | scrittura confermata | creazione atomica dei tre file sorgente dopo conferma esplicita |
| `rollback_project_creation` | scrittura confermata | rimozione dei file creati solo se gli hash post-apply coincidono |

### Decisioni e criteri di accettazione

- [x] Nome e numero di layer sono input Pydantic limitati; non entra sintassi KiCad libera.
- [x] Lo schematico nasce da `kicad-sch-api` e il PCB dall'API `pcbnew` distribuita con KiCad.
- [x] Una directory target non vuota viene rifiutata; e ammessa soltanto la preview Copperbrain.
- [x] Prima della conferma nessun `.kicad_pro`, `.kicad_sch` o `.kicad_pcb` appare nel target.
- [x] Apply e rollback sono atomici e il rollback rifiuta file modificati dopo l'applicazione.
- [x] Il manifest Pydantic e persistito nello storage privato e consente validate/apply/rollback
  dopo il riavvio del server senza fidarsi di percorsi esterni.

## 19. Estensione approvata — benchmark motore 12 V / 20 A circoscritto

La richiesta del 16 luglio 2026 approva un solo reference design deterministico per il progetto
`test_bench_pico`; non introduce generazione autonoma e illimitata di circuiti. Il template usa
soltanto operazioni schematiche semantiche, modelli di regole e un piano di placement tipizzato.

### Topologia e assunzioni revisionabili

- motore DC brushed bidirezionale, 12 V nominali e target provvisorio 20 A continui;
- DRV8701 con quattro MOSFET esterni CSD18540Q5B, shunt Kelvin da 1 mOhm e fusibile ATO 25 A;
- ATtiny1616 alimentato dal DVDD del driver, UPDI, fault/current feedback e LED di stato;
- RS-485 half-duplex THVD1429 con SM712 e terminazione 120 ohm inseribile;
- quattro ingressi digitali campo 5–24 V protetti con optoisolatore, non quattro uscite;
- `PGND` di potenza separata dalla `GND` logica e unita in un solo punto 0 ohm vicino allo shunt;
- PCB 120 x 100 mm, due layer, rame esterno assunto 70 um, incremento termico ammesso 20 C.

Le assunzioni restano provvisorie finche l'utente non conferma tensione, corrente continua/stallo,
tipo motore, natura degli I/O e stackup. Il template non e una certificazione di produzione.

La creazione di un nuovo progetto usa due layer di rame per default. Un singolo layer e riservato
a progetti esplicitamente minimali e non e generato dal workflow corrente; quattro layer richiedono
una richiesta esplicita. Il routing controllato opera comunque soltanto su `F.Cu` e `B.Cu`.

### Gate e limiti

- [x] Schematico e layout nascono da API/operazioni tipizzate; nessuna S-expression e scritta
  direttamente dal modello.
- [x] `multiple_net_names` e una regressione ERC bloccante anche se KiCad la classifica warning.
- [x] Le geometrie footprint considerano rotazione, primitive custom e pad diversi sulla stessa
  rete prima di derivare fanout e clearance.
- [x] La preview combinata deve avere ERC senza errori, DRC senza violazioni e placement 100.
- [x] Il placement benchmark e stato rigenerato con distanza ratsnest, rotazioni ortogonali,
  corridoi di routing e bottom per soli passivi: area outline ridotta del 29% e ratsnest stimata
  ridotta del 46% rispetto al piano 140 x 120 mm precedente, senza errori DRC sul benchmark.
- [x] Le reti 20 A conservano la larghezza preferita calcolata (6,15 mm con le assunzioni sopra);
  il vincolo non viene ridotto per forzare un risultato dell'autorouter.
- [x] Se FreeRouting non produce un candidato completo entro il watchdog, il PCB resta
  intenzionalmente unrouted e non applicabile come finalizzazione.
- [ ] Zone/poligoni di rame, verifica termica/SOA, stackup, EMC/SI/PI, DFM e routing finale sono
  richiesti prima di qualsiasi dichiarazione `production_ready`; le zone restano fuori dall'MVP.
- [ ] Ogni apply al progetto sorgente richiede conferma esplicita, editor chiuso, hash non stale,
  snapshot e rollback secondo i workflow gia approvati.
