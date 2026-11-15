"""The make-believe firm the sample year is built from: what it buys, where it works.

The names follow the real paperwork the app was written for — a metalwork and glazing
shop around Bordeaux — so the screens read the way they do in earnest. Nothing here
belongs to a real company: the suppliers are the trade names the notes use, and every
price is invented.
"""

# article, designation, supplier, usual quantity on a note, price a piece
ARTICLES = [
    ("CHEVILLE SPIT FIX", "343546 - CHEVILLE SPIT FIX 3 10X95/36-26MT BTE50", "NORTHGATE", 100, 0.62),
    ("GOUJON FGA M10", "300760 - GOUJON TECHPRO FGA M10X90 B100", "NORTHGATE", 200, 0.48),
    ("VIS TH 6X25", "316519 - VIS TH 6X25 DIN933 INOX A2 BTE 200", "NORTHGATE", 400, 0.11),
    ("VIS METAUX BHC 6X30", "272001 - VIS METAUX BHC 6X30 INOX A4 B200", "NORTHGATE", 400, 0.19),
    ("ECROU HU 10", "118190 - ECROU HU 10 ACIER BRUT B100", "NORTHGATE", 500, 0.06),
    ("ECROU HU 12", "296201 - ECROUS HU 12 ZN B50", "NORTHGATE", 250, 0.09),
    ("RONDELLE M8", "335160 - RONDELLE SERIE M 08X18X1.5 ZING BTE 500", "NORTHGATE", 500, 0.03),
    ("RONDELLE M6", "335140 - RONDELLE SERIE M 06X14X1.2 ZING BTE 500", "NORTHGATE", 500, 0.02),
    ("CACHE ECROU M6", "316757 - CACHE ECROU NYLON M6 BLC BTE 100", "NORTHGATE", 600, 0.04),
    ("CACHE ECROU M10", "316785 - CACHE ECROU NYLON M10 BLC BTE 100", "NORTHGATE", 400, 0.05),
    ("CACHE ECROU M14", "CE14SW22G - Cache ecrou M14 Cle 22 mm - Gris", "NORTHGATE", 100, 0.08),
    ("TAMIS NYLON 12X80", "151710 - TAMIS NYLON 12X80 20PCS TECHPRO", "NORTHGATE", 40, 0.35),
    ("RESINE POLYESTER BEIGE", "315248 - RESINE POLYESTER 410ML BEIGE", "NORTHGATE", 6, 14.90),
    ("PAUMELLE MAROC A SOUDER", "128786 - PAUMELLE MAROC A SOUDER HT120MM", "NORTHGATE", 12, 7.40),
    ("SERRURE FIFTYLOCK", "008453 - SERRURE FIFTYLOCK INOX AXE 30MM", "NORTHGATE", 2, 118.00),
    ("PLAQUE SERRURE LOCINOX", "147194 - PLAQUE P/SERR LOCINOX CLE I EA92 X2 ARGT", "NORTHGATE", 3, 42.50),
    ("ENSEMBLE DOUBLE BEQUILLE", "NFL80488 - Pdr ENS.DOUBLE BEQ. CLEI INOX Novo110", "NOVAHARD", 2, 86.00),
    ("FERME PORTE GR3400", "053571 - FERME PORTE GR3400 EN3/4 COULISSE ARGT", "NOVAHARD", 4, 134.00),
    ("BOITIER CREMONE POMPIER", "096334 - BOITIER CREMONE POMPIER DEESSE NOIR", "NOVAHARD", 1, 96.00),
    ("JEU TRINGLE NOIR", "096313 - JEU TRINGLE 7231 NOIR 9005 2X1250MM", "NOVAHARD", 2, 38.00),

    ("PROFIL EN U POINCONNE GALVA", "C5 - Profil en U poinconne Lg 1900 Dev 150 Tole DX51D Galva Z275 30/10",
     "FORMA STEEL", 4, 76.00),
    ("PLAT POINCONNE GALVA", "A14 - Plat poinconne Lg 2077 Dev 798 Tole DX51D Galva Z275 15/10",
     "FORMA STEEL", 2, 54.00),
    ("ECLISSE 370X200 RAL 9010", "Piece Cisaillee Pliee 370 x 200 mm Laque 75/100 RAL 9010 4 plis",
     "FORMA STEEL", 8, 18.50),
    ("COUVERTINE 3000X370 RAL 9010", "Piece Cisaillee Pliee 3000 x 370 mm Laque 75/100 RAL 9010 4 plis",
     "FORMA STEEL", 8, 62.00),
    ("TOLE DX51D 20/10", "Tole Acier DX51D Galva Z275 2000x1000 20/10", "METALWORKS", 6, 88.00),
    ("CORNIERE 40X40", "Corniere acier 40x40x4 Lg 6000", "METALWORKS", 10, 23.40),
    ("TUBE 40X40 EP2", "Tube carre acier 40x40 ep.2 Lg 6000", "METALWORKS", 12, 31.00),
    ("PLATINE 150X150", "Platine acier 150x150 ep.10 percee 4 trous", "CUTSTEEL", 20, 9.80),
    ("PIECE ALU 955X60", "Rep4 - Piece 955x60 ep 6 Alu 5754H111", "CUTSTEEL", 15, 27.00),
    ("CORNIERE ALU 30X30", "Corniere alu 30x30x3 Lg 6000 brut", "VARCO", 8, 19.60),

    ("STADIP PROTECT 44.2", "STADIP PROTECT 44.2 - JPP 4 COTES", "CLEARVIEW GLASS", 2, 148.00),
    ("STADIP PROTECT 44.2 OPALE", "STADIP PROTECT 44.2 OPALE - JPP 2 COTES", "CLEARVIEW GLASS", 2, 176.00),
    ("VITRAGE 4/16/4 ARGON", "Double vitrage 4/16/4 argon warm edge", "CLEARVIEW GLASS", 3, 132.00),
    ("JOINT EPDM 4MM", "Joint EPDM noir 4mm couronne 50m", "JOINT TWIN", 4, 46.00),
    ("SILICONE NEUTRE NOIR", "Cartouche silicone neutre noir 310ml", "PLASTOR", 24, 7.20),
    ("MOUSSE PU 750ML", "Mousse PU pistolable 750ml", "PLASTOR", 12, 8.60),

    ("COLORVIS RAL 9006", "285620010 - COLORVIS 4PI TH8 Zn 5.5x25G14 RAL 9006 (100p)", "LR SEALTEC", 200, 0.21),
    ("BELVIS RAL 7016", "395353236 - BELVIS 4T 5.5x25 RAL 7016 (100p)", "LR SEALTEC", 200, 0.24),
    ("PRESPIN TYPE N", "358430000 - PRESPIN Type N Tige 2.7 Lg.114mm (500p)", "LR SEALTEC", 500, 0.14),
    ("RIVET ALU 4.8X12", "Rivet aveugle alu/acier 4.8x12 boite 500", "WESTFIX", 500, 0.07),
    ("DISQUE A TRONCONNER 125", "Disque a tronconner inox 125x1.0 boite 25", "WESTFIX", 50, 1.90),
    ("FORET HSS 6MM", "Foret HSS-G 6mm boite 10", "WESTFIX", 30, 2.10),
    ("GANT MANUTENTION T9", "Gant manutention enduit PU taille 9", "LINKOFFICE", 60, 2.40),
    ("LUNETTE DE PROTECTION", "Lunette de protection incolore anti-rayure", "LINKOFFICE", 24, 4.10),
    ("PEINTURE RAL 9010 25KG", "Poudre polyester RAL 9010 brillant seau 25kg", "AKRON PAINTS", 2, 186.00),
    ("PEINTURE RAL 7016 25KG", "Poudre polyester RAL 7016 mat seau 25kg", "AKRON PAINTS", 2, 192.00),
    ("DEGRAISSANT ALU 20L", "Degraissant alcalin aluminium bidon 20L", "BERNAY ALKOR", 2, 94.00),
    ("ZINGAGE BAIN", "Galvanisation a chaud au bain - lot", "ZINCO", 1, 310.00),
    ("PALETTE CONSIGNEE", "Palette bois consignee 1200x800", "MARTEL", 10, 18.00),
    ("FILM ETIRABLE 20U", "Film etirable manuel 450mm 20 microns", "PROMARK", 18, 4.80),
]

SITES = [
    ("LE PRIEURE", ["PHASE 3", "PORTE C003", "CELLIER TERRASSE BAT C", "SEPARATIFS", "GC"]),
    ("CORAIL", ["PORTAIL BAT C", "GC ATTIQUE BAT. A", "SEPARATIFS BAT. A"]),
    ("ALBATROS", ["ESCALIER", "ECROU ACOUSTIQUE", "COMMANDE FIXATION"]),
    ("SQUARE DES ORMES", ["BAT B", "GC BALCONS BAT C", "GC FENETRES BAT C"]),
    ("PLACE NEUVE", ["PM", "PM ET GC"]),
    ("LES PEUPLIERS", ["PM ET PAC", "PORTE PAC", "PORTES GAINE TECH"]),
    ("ECRIN DES QUAIS", ["HABILLAGE JARDINIERE"]),
    ("CADENCE", ["VITRAGE", "VITRE"]),
    ("TRESSES", ["COUVERTINES"]),
    ("1003", ["ATELIER", "STOCK"]),
    ("LETTRES", ["GRILLE RESEAUX"]),
    ("LA BASTIDE", ["GC TYPE 02"]),
    ("VIA VERDE", ["GC"]),
    ("TURENNE", ["PM"]),
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

DRAFTERS = ["ALEX", "MARIE", "SAM", "LEO"]

# Where the lorries go: the depot first, then the day's addresses.
DEPOT = ("THE YARD", "8 rue des Forges, 33520 BRUGES", 44.8801, -0.6102)
STOPS = [
    ("LE PRIEURE", "3 rue du Prieure, 33000 BORDEAUX", 44.8402, -0.5801),
    ("CORAIL", "10 quai des Mouettes, 33100 BORDEAUX", 44.8560, -0.5420),
    ("ALBATROS", "21 avenue des Pins, 33700 MERIGNAC", 44.835, -0.645),
    ("SQUARE DES ORMES", "5 square des Ormes, 33000 BORDEAUX", 44.8310, -0.5690),
    ("PLACE NEUVE", "9 place Neuve, 33000 BORDEAUX", 44.84, -0.59),
    ("LES PEUPLIERS", "25 allee des Peupliers, 33300 BORDEAUX", 44.8650, -0.5640),
    ("ECRIN DES QUAIS", "40 cours des Quais, 33300 BORDEAUX", 44.8620, -0.5750),
    ("CADENCE", "6 rue de la Cadence, 33300 BORDEAUX", 44.8550, -0.5600),
    ("TRESSES", "5 avenue de l'Industrie, 33370 TRESSES", 44.8440, -0.4990),
    ("2001 ATELIER", "8 rue des Forges, 33520 BRUGES", 44.8801, -0.6102),
    ("LETTRES", "14 cours des Lettres, 33400 TALENCE", 44.8100, -0.5950),
    ("LA BASTIDE", "14 rue de la Bastide, 33100 BORDEAUX", 44.8395, -0.5487),
]
