"""The make-believe firm the sample year is built from: what it buys, where it works.

The names follow the real paperwork the app was written for — a metalwork and glazing
shop around Bordeaux — so the screens read the way they do in earnest. Nothing here
belongs to a real company: the suppliers are the trade names the notes use, and every
price is invented.
"""

# article, designation, supplier, usual quantity on a note, price a piece
ARTICLES = [
    ("CHEVILLE SPIT FIX", "343546 - CHEVILLE SPIT FIX 3 10X95/36-26MT BTE50", "FOUSSIER", 100, 0.62),
    ("GOUJON FGA M10", "300760 - GOUJON TECHPRO FGA M10X90 B100", "FOUSSIER", 200, 0.48),
    ("VIS TH 6X25", "316519 - VIS TH 6X25 DIN933 INOX A2 BTE 200", "FOUSSIER", 400, 0.11),
    ("VIS METAUX BHC 6X30", "272001 - VIS METAUX BHC 6X30 INOX A4 B200", "FOUSSIER", 400, 0.19),
    ("ECROU HU 10", "118190 - ECROU HU 10 ACIER BRUT B100", "FOUSSIER", 500, 0.06),
    ("ECROU HU 12", "296201 - ECROUS HU 12 ZN B50", "FOUSSIER", 250, 0.09),
    ("RONDELLE M8", "335160 - RONDELLE SERIE M 08X18X1.5 ZING BTE 500", "FOUSSIER", 500, 0.03),
    ("RONDELLE M6", "335140 - RONDELLE SERIE M 06X14X1.2 ZING BTE 500", "FOUSSIER", 500, 0.02),
    ("CACHE ECROU M6", "316757 - CACHE ECROU NYLON M6 BLC BTE 100", "FOUSSIER", 600, 0.04),
    ("CACHE ECROU M10", "316785 - CACHE ECROU NYLON M10 BLC BTE 100", "FOUSSIER", 400, 0.05),
    ("CACHE ECROU M14", "CE14SW22G - Cache ecrou M14 Cle 22 mm - Gris", "FOUSSIER", 100, 0.08),
    ("TAMIS NYLON 12X80", "151710 - TAMIS NYLON 12X80 20PCS TECHPRO", "FOUSSIER", 40, 0.35),
    ("RESINE POLYESTER BEIGE", "315248 - RESINE POLYESTER 410ML BEIGE", "FOUSSIER", 6, 14.90),
    ("PAUMELLE MAROC A SOUDER", "128786 - PAUMELLE MAROC A SOUDER HT120MM", "FOUSSIER", 12, 7.40),
    ("SERRURE FIFTYLOCK", "008453 - SERRURE FIFTYLOCK INOX AXE 30MM", "FOUSSIER", 2, 118.00),
    ("PLAQUE SERRURE LOCINOX", "147194 - PLAQUE P/SERR LOCINOX CLE I EA92 X2 ARGT", "FOUSSIER", 3, 42.50),
    ("ENSEMBLE DOUBLE BEQUILLE", "NFL80488 - Pdr ENS.DOUBLE BEQ. CLEI INOX Novo110", "NOVOFERM", 2, 86.00),
    ("FERME PORTE GR3400", "053571 - FERME PORTE GR3400 EN3/4 COULISSE ARGT", "NOVOFERM", 4, 134.00),
    ("BOITIER CREMONE POMPIER", "096334 - BOITIER CREMONE POMPIER DEESSE NOIR", "NOVOFERM", 1, 96.00),
    ("JEU TRINGLE NOIR", "096313 - JEU TRINGLE 7231 NOIR 9005 2X1250MM", "NOVOFERM", 2, 38.00),

    ("PROFIL EN U POINCONNE GALVA", "C5 - Profil en U poinconne Lg 1900 Dev 150 Tole DX51D Galva Z275 30/10",
     "BATI FORMES", 4, 76.00),
    ("PLAT POINCONNE GALVA", "A14 - Plat poinconne Lg 2077 Dev 798 Tole DX51D Galva Z275 15/10",
     "BATI FORMES", 2, 54.00),
    ("ECLISSE 370X200 RAL 9010", "Piece Cisaillee Pliee 370 x 200 mm Laque 75/100 RAL 9010 4 plis",
     "BATI FORMES", 8, 18.50),
    ("COUVERTINE 3000X370 RAL 9010", "Piece Cisaillee Pliee 3000 x 370 mm Laque 75/100 RAL 9010 4 plis",
     "BATI FORMES", 8, 62.00),
    ("TOLE DX51D 20/10", "Tole Acier DX51D Galva Z275 2000x1000 20/10", "METALECO", 6, 88.00),
    ("CORNIERE 40X40", "Corniere acier 40x40x4 Lg 6000", "METALECO", 10, 23.40),
    ("TUBE 40X40 EP2", "Tube carre acier 40x40 ep.2 Lg 6000", "METALECO", 12, 31.00),
    ("PLATINE 150X150", "Platine acier 150x150 ep.10 percee 4 trous", "DECOUP'ACIER", 20, 9.80),
    ("PIECE ALU 955X60", "Rep4 - Piece 955x60 ep 6 Alu 5754H111", "DECOUP'ACIER", 15, 27.00),
    ("CORNIERE ALU 30X30", "Corniere alu 30x30x3 Lg 6000 brut", "VACO", 8, 19.60),

    ("STADIP PROTECT 44.2", "STADIP PROTECT 44.2 - JPP 4 COTES", "SAINT-GOBAIN VITRAGE", 2, 148.00),
    ("STADIP PROTECT 44.2 OPALE", "STADIP PROTECT 44.2 OPALE - JPP 2 COTES", "SAINT-GOBAIN VITRAGE", 2, 176.00),
    ("VITRAGE 4/16/4 ARGON", "Double vitrage 4/16/4 argon warm edge", "SAINT-GOBAIN VITRAGE", 3, 132.00),
    ("JOINT EPDM 4MM", "Joint EPDM noir 4mm couronne 50m", "JOINT DUAL", 4, 46.00),
    ("SILICONE NEUTRE NOIR", "Cartouche silicone neutre noir 310ml", "PLASTEM", 24, 7.20),
    ("MOUSSE PU 750ML", "Mousse PU pistolable 750ml", "PLASTEM", 12, 8.60),

    ("COLORVIS RAL 9006", "285620010 - COLORVIS 4PI TH8 Zn 5.5x25G14 RAL 9006 (100p)", "LR ETANCO", 200, 0.21),
    ("BELVIS RAL 7016", "395353236 - BELVIS 4T 5.5x25 RAL 7016 (100p)", "LR ETANCO", 200, 0.24),
    ("PRESPIN TYPE N", "358430000 - PRESPIN Type N Tige 2.7 Lg.114mm (500p)", "LR ETANCO", 500, 0.14),
    ("RIVET ALU 4.8X12", "Rivet aveugle alu/acier 4.8x12 boite 500", "WURTH", 500, 0.07),
    ("DISQUE A TRONCONNER 125", "Disque a tronconner inox 125x1.0 boite 25", "WURTH", 50, 1.90),
    ("FORET HSS 6MM", "Foret HSS-G 6mm boite 10", "WURTH", 30, 2.10),
    ("GANT MANUTENTION T9", "Gant manutention enduit PU taille 9", "LYRECO", 60, 2.40),
    ("LUNETTE DE PROTECTION", "Lunette de protection incolore anti-rayure", "LYRECO", 24, 4.10),
    ("PEINTURE RAL 9010 25KG", "Poudre polyester RAL 9010 brillant seau 25kg", "AKZO NOBEL", 2, 186.00),
    ("PEINTURE RAL 7016 25KG", "Poudre polyester RAL 7016 mat seau 25kg", "AKZO NOBEL", 2, 192.00),
    ("DEGRAISSANT ALU 20L", "Degraissant alcalin aluminium bidon 20L", "BOUCHILLOU ALKYA", 2, 94.00),
    ("ZINGAGE BAIN", "Galvanisation a chaud au bain - lot", "ZINQ", 1, 310.00),
    ("PALETTE CONSIGNEE", "Palette bois consignee 1200x800", "MOTTEZ", 10, 18.00),
    ("FILM ETIRABLE 20U", "Film etirable manuel 450mm 20 microns", "PROLIANS", 18, 4.80),
]

SITES = [
    ("LA GENETTE", ["PHASE 3", "PORTE C003", "CELLIER TERRASSE BAT C", "SEPARATIFS", "GC"]),
    ("OSTREA", ["PORTAIL BAT C", "GC ATTIQUE BAT. A", "SEPARATIFS BAT. A"]),
    ("ARIANE", ["ESCALIER", "ECROU ACOUSTIQUE", "COMMANDE FIXATION"]),
    ("SQUARE DE LA MARNE", ["BAT B", "GC BALCONS BAT C", "GC FENETRES BAT C"]),
    ("MARC NOUAUX", ["PM", "PM ET GC"]),
    ("LES GRAVELOTS", ["PM ET PAC", "PORTE PAC", "PORTES GAINE TECH"]),
    ("ECRIN DES CHARTRONS", ["HABILLAGE JARDINIERE"]),
    ("PULSATION", ["VITRAGE", "VITRE"]),
    ("ARTIGUES", ["COUVERTINES"]),
    ("1003", ["ATELIER", "STOCK"]),
    ("HUMANITIES", ["GRILLE RESEAUX"]),
    ("LA BENAUGE", ["GC TYPE 02"]),
    ("VIA VIGNA", ["GC"]),
    ("TASSIGNY", ["PM"]),
]

# A quote request asks for what is not bought by the piece.
QUOTE_LINES = [
    ("PLINTHE ALU ANODISE", 120, "ml"),
    ("GRAVE 0/31.5", 3, "m3"),
    ("TOLE LARMEE 3MM", 18, "m2"),
    ("PROFIL ALU 50X30", 240, "ml"),
    ("BETON FIBRE", 2, "m3"),
    ("ACIER INOX 304 BOBINE", 1.2, "t"),
    ("THERMOLAQUAGE RAL 9005", 46, "m2"),
    ("POSE ET REGLAGE", 16, "h"),
    ("DEGRAISSANT", 60, "l"),
    ("GARDE CORPS VITRE", 9, "ml"),
]

DRAFTERS = ["ALEX", "MARIE", "SOFIANE", "LUC"]

# Where the lorries go: the depot first, then the day's addresses.
DEPOT = ("THE YARD", "12 rue des Ateliers, 33520 BRUGES", 44.8876, -0.6008)
STOPS = [
    ("LA GENETTE", "44 rue de la Genette, 33000 BORDEAUX", 44.8352, -0.5725),
    ("OSTREA", "2 quai de Brazza, 33100 BORDEAUX", 44.8515, -0.5471),
    ("ARIANE", "18 avenue Ariane, 33700 MERIGNAC", 44.8307, -0.6504),
    ("SQUARE DE LA MARNE", "7 square de la Marne, 33000 BORDEAUX", 44.8263, -0.5631),
    ("MARC NOUAUX", "9 rue Marc Nouaux, 33000 BORDEAUX", 44.8458, -0.5856),
    ("LES GRAVELOTS", "25 rue des Gravelots, 33300 BORDEAUX", 44.8691, -0.5583),
    ("ECRIN DES CHARTRONS", "61 cours Saint-Louis, 33300 BORDEAUX", 44.8577, -0.5693),
    ("PULSATION", "3 rue Lucien Faure, 33300 BORDEAUX", 44.8606, -0.5531),
    ("ARTIGUES", "5 avenue de l'Industrie, 33370 ARTIGUES", 44.8495, -0.4938),
    ("1003 ATELIER", "12 rue des Ateliers, 33520 BRUGES", 44.8876, -0.6008),
    ("HUMANITIES", "30 cours de la Liberation, 33400 TALENCE", 44.8060, -0.5900),
    ("LA BENAUGE", "14 rue de la Benauge, 33100 BORDEAUX", 44.8395, -0.5487),
]
