-- Skema Lengkap Database PostgreSQL untuk AKMv2 (Koper Sakti UDINUS x Dinkes)

DO $$ BEGIN
    CREATE TYPE user_role AS ENUM ('admin', 'operator', 'pasien');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    nama VARCHAR(255) NOT NULL,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    no_hp VARCHAR(20) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role user_role NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS patients (
    id SERIAL PRIMARY KEY,
    nama_lengkap VARCHAR(255) NOT NULL,
    jenis_kelamin VARCHAR(20),
    alamat TEXT,
    umur INT,
    nik VARCHAR(20) UNIQUE,
    no_hp VARCHAR(20),
    tanggal_lahir DATE,
    email VARCHAR(255),
    provinsi VARCHAR(100),
    kecamatan VARCHAR(100),
    kelurahan VARCHAR(100),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_patients_nama ON patients(nama_lengkap);
CREATE INDEX IF NOT EXISTS idx_patients_nik ON patients(nik);

CREATE TABLE IF NOT EXISTS measurements (
    id SERIAL PRIMARY KEY,
    patient_id INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    user_id INT NOT NULL REFERENCES users(id),
    cholesterol_value FLOAT DEFAULT 0,
    uric_acid_value FLOAT DEFAULT 0,
    blood_sugar_value FLOAT DEFAULT 0,
    blood_pressure_sys INT DEFAULT 0,
    blood_pressure_dia INT DEFAULT 0,
    blood_pressure_pulse INT DEFAULT 0,
    bp_source VARCHAR(30) DEFAULT 'sensor',
    sensor_bp_sys INT DEFAULT 0,
    sensor_bp_dia INT DEFAULT 0,
    sensor_bp_pulse INT DEFAULT 0,
    omron_bp_sys INT DEFAULT 0,
    omron_bp_dia INT DEFAULT 0,
    omron_bp_pulse INT DEFAULT 0,
    spo2_value FLOAT DEFAULT 0,
    temperature_value FLOAT DEFAULT 0,
    height FLOAT DEFAULT 0,
    weight FLOAT DEFAULT 0,
    measured_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS raw_sensor_data (
    id SERIAL PRIMARY KEY,
    patient_id INT REFERENCES patients(id) ON DELETE SET NULL,
    measurement_id INT REFERENCES measurements(id) ON DELETE SET NULL,
    sensor_type VARCHAR(50) NOT NULL,
    raw_values JSONB,
    raw_frames JSONB,
    context_features JSONB,
    predicted_value FLOAT,
    ground_truth_value FLOAT,
    ground_truth_source VARCHAR(100),
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_raw_sensor_type ON raw_sensor_data(sensor_type);
CREATE INDEX IF NOT EXISTS idx_raw_sensor_gt ON raw_sensor_data(sensor_type) WHERE ground_truth_value IS NOT NULL;

CREATE TABLE IF NOT EXISTS system_config (
    key VARCHAR(50) PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS api_history (
    url TEXT PRIMARY KEY,
    last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS puskesmas (
    id SERIAL PRIMARY KEY,
    nama VARCHAR(150) UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS master_provinsis (
    id SERIAL PRIMARY KEY,
    nama VARCHAR(150) UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS master_kecamatans (
    kode_kecamatan VARCHAR(20) PRIMARY KEY,
    nama VARCHAR(150) NOT NULL
);

CREATE TABLE IF NOT EXISTS master_kelurahans (
    id SERIAL PRIMARY KEY,
    kode_parent VARCHAR(20) REFERENCES master_kecamatans(kode_kecamatan) ON DELETE CASCADE,
    nama VARCHAR(150) NOT NULL
);

-- Seed 38 Provinsi Indonesia
INSERT INTO master_provinsis (nama) VALUES
('Aceh'), ('Sumatera Utara'), ('Sumatera Barat'), ('Riau'), ('Kepulauan Riau'),
('Jambi'), ('Sumatera Selatan'), ('Kepulauan Bangka Belitung'), ('Bengkulu'), ('Lampung'),
('DKI Jakarta'), ('Banten'), ('Jawa Barat'), ('Jawa Tengah'), ('DI Yogyakarta'),
('Jawa Timur'), ('Bali'), ('Nusa Tenggara Barat'), ('Nusa Tenggara Timur'), ('Kalimantan Barat'),
('Kalimantan Tengah'), ('Kalimantan Selatan'), ('Kalimantan Timur'), ('Kalimantan Utara'), ('Sulawesi Utara'),
('Gorontalo'), ('Sulawesi Tengah'), ('Sulawesi Barat'), ('Sulawesi Selatan'), ('Sulawesi Tenggara'),
('Maluku'), ('Maluku Utara'), ('Papua'), ('Papua Barat'), ('Papua Selatan'),
('Papua Tengah'), ('Papua Pegunungan'), ('Papua Barat Daya')
ON CONFLICT (nama) DO NOTHING;

-- Seed 16 Kecamatan Kota Semarang
INSERT INTO master_kecamatans (kode_kecamatan, nama) VALUES
('337401', 'Mijen'),
('337402', 'Gunungpati'),
('337403', 'Banyumanik'),
('337404', 'Gajahmungkur'),
('337405', 'Semarang Selatan'),
('337406', 'Candisari'),
('337407', 'Tembalang'),
('337408', 'Pedurungan'),
('337409', 'Genuk'),
('337410', 'Gayamsari'),
('337411', 'Semarang Timur'),
('337412', 'Semarang Utara'),
('337413', 'Semarang Tengah'),
('337414', 'Semarang Barat'),
('337415', 'Tugu'),
('337416', 'Ngaliyan')
ON CONFLICT (kode_kecamatan) DO NOTHING;

-- Seed 177 Kelurahan Kota Semarang
INSERT INTO master_kelurahans (kode_parent, nama) VALUES
('337401', 'Bubakan'), ('337401', 'Cangkiran'), ('337401', 'Jatibarang'), ('337401', 'Jatisari'), ('337401', 'Karangmalang'), ('337401', 'Kedungpane'), ('337401', 'Mijen'), ('337401', 'Ngadirgo'), ('337401', 'Pesantren'), ('337401', 'Polaman'), ('337401', 'Purwosari'), ('337401', 'Tambangan'), ('337401', 'Wonolopo'), ('337401', 'Wonoplumbon'),
('337402', 'Cepoko'), ('337402', 'Gunungpati'), ('337402', 'Jatirejo'), ('337402', 'Kalisegoro'), ('337402', 'Kandri'), ('337402', 'Mangunsari'), ('337402', 'Ngijo'), ('337402', 'Nongkosawit'), ('337402', 'Pakintelan'), ('337402', 'Patemon'), ('337402', 'Plalangan'), ('337402', 'Pongangan'), ('337402', 'Sadeng'), ('337402', 'Sekaran'), ('337402', 'Sukorejo'), ('337402', 'Sumurejo'),
('337403', 'Banyumanik'), ('337403', 'Gedawang'), ('337403', 'Jabungan'), ('337403', 'Ngesrep'), ('337403', 'Padangsari'), ('337403', 'Pedalangan'), ('337403', 'Pudakpayung'), ('337403', 'Srondol Kulon'), ('337403', 'Srondol Wetan'), ('337403', 'Sumurboto'), ('337403', 'Tinjomoyo'),
('337404', 'Bendan Duwur'), ('337404', 'Bendan Ngisor'), ('337404', 'Bendungan'), ('337404', 'Gajahmungkur'), ('337404', 'Karangrejo'), ('337404', 'Lempongsari'), ('337404', 'Petompon'), ('337404', 'Sampangan'),
('337405', 'Barusari'), ('337405', 'Bulustalan'), ('337405', 'Lamper Kidul'), ('337405', 'Lamper Lor'), ('337405', 'Lamper Tengah'), ('337405', 'Mugassari'), ('337405', 'Peterongan'), ('337405', 'Pleburan'), ('337405', 'Randusari'), ('337405', 'Wonodri'),
('337406', 'Candi'), ('337406', 'Jatingaleh'), ('337406', 'Jomblang'), ('337406', 'Kaliwiru'), ('337406', 'Karanganyar Gunung'), ('337406', 'Tegalsari'), ('337406', 'Wonotingal'),
('337407', 'Bulusan'), ('337407', 'Jangli'), ('337407', 'Kedungmundu'), ('337407', 'Kramas'), ('337407', 'Mangunharjo'), ('337407', 'Meteseh'), ('337407', 'Rowosari'), ('337407', 'Sambiroto'), ('337407', 'Sendangguwo'), ('337407', 'Sendangmulyo'), ('337407', 'Tandang'), ('337407', 'Tembalang'),
('337408', 'Gemah'), ('337408', 'Kalicari'), ('337408', 'Muktiharjo Kidul'), ('337408', 'Palebon'), ('337408', 'Pedurungan Kidul'), ('337408', 'Pedurungan Lor'), ('337408', 'Pedurungan Tengah'), ('337408', 'Penggaron Kidul'), ('337408', 'Plamongan Sari'), ('337408', 'Tlogomulyo'), ('337408', 'Tlogosari Kulon'), ('337408', 'Tlogosari Wetan'),
('337409', 'Bangetayu Kulon'), ('337409', 'Bangetayu Wetan'), ('337409', 'Banjardowo'), ('337409', 'Gebangsari'), ('337409', 'Genuksari'), ('337409', 'Karangroto'), ('337409', 'Kudu'), ('337409', 'Muktiharjo Lor'), ('337409', 'Penggaron Lor'), ('337409', 'Sembungharjo'), ('337409', 'Terboyo Kulon'), ('337409', 'Terboyo Wetan'), ('337409', 'Trimulyo'),
('337410', 'Gayamsari'), ('337410', 'Kaligawe'), ('337410', 'Pandean Lamper'), ('337410', 'Sambirejo'), ('337410', 'Sawahbesar'), ('337410', 'Siwalan'), ('337410', 'Tambakrejo'),
('337411', 'Bugangan'), ('337411', 'Karangtempel'), ('337411', 'Karangturi'), ('337411', 'Kebonagung'), ('337411', 'Kemijen'), ('337411', 'Mlatibaru'), ('337411', 'Mlatiharjo'), ('337411', 'Rejomulyo'), ('337411', 'Rejosari'), ('337411', 'Sarirejo'),
('337412', 'Bandarharjo'), ('337412', 'Bulu Lor'), ('337412', 'Dadapsari'), ('337412', 'Kuningan'), ('337412', 'Panggung Kidul'), ('337412', 'Panggung Lor'), ('337412', 'Plombokan'), ('337412', 'Purwosari'), ('337412', 'Tanjung Mas'),
('337413', 'Bangunharjo'), ('337413', 'Brumbungan'), ('337413', 'Gabahan'), ('337413', 'Jagalan'), ('337413', 'Karangkidul'), ('337413', 'Kauman'), ('337413', 'Kembangsari'), ('337413', 'Kranggan'), ('337413', 'Miroto'), ('337413', 'Pandansari'), ('337413', 'Pekunden'), ('337413', 'Pendrikan Kidul'), ('337413', 'Pendrikan Lor'), ('337413', 'Purwodinatan'), ('337413', 'Sekayu'),
('337414', 'Bojongsalaman'), ('337414', 'Bongsari'), ('337414', 'Cabean'), ('337414', 'Gisikdrono'), ('337414', 'Kalibanteng Kidul'), ('337414', 'Kalibanteng Kulon'), ('337414', 'Karangayu'), ('337414', 'Kembangarum'), ('337414', 'Krapyak'), ('337414', 'Krobokan'), ('337414', 'Manyaran'), ('337414', 'Ngemplak Simongan'), ('337414', 'Salamanmloyo'), ('337414', 'Tambakharjo'), ('337414', 'Tawangmas'), ('337414', 'Tawangsari'),
('337415', 'Jerakah'), ('337415', 'Karanganyar'), ('337415', 'Mangkang Kulon'), ('337415', 'Mangkang Wetan'), ('337415', 'Mangunharjo'), ('337415', 'Randugarut'), ('337415', 'Tugurejo'),
('337416', 'Bambankerep'), ('337416', 'Bringin'), ('337416', 'Gondoriyo'), ('337416', 'Kalipancur'), ('337416', 'Ngaliyan'), ('337416', 'Podorejo'), ('337416', 'Purwoyoso'), ('337416', 'Tambakaji'), ('337416', 'Wates'), ('337416', 'Wonosari');

-- Seed 39 Puskesmas Resmi Kota Semarang
INSERT INTO puskesmas (nama) VALUES
('Bandarharjo'), ('Bangetayu'), ('Bulu Lor'), ('Candilama'), ('Gayamsari'),
('Genuk'), ('Gunungpati'), ('Halmahera'), ('Jabungan'), ('Karanganyar'),
('Karangayu'), ('Karangdoro'), ('Karangmalang'), ('Kedungmundu'), ('Krobokan'),
('Lamper Tengah'), ('Lebdosari'), ('Mangkang'), ('Manyaran'), ('Mijen'),
('Miroto'), ('Ngaliyan'), ('Ngesrep'), ('Padangsari'), ('Pandanaran'),
('Pegandan'), ('Plamongan Sari'), ('Poncol'), ('Pudakpayung'), ('Purwoyoso'),
('Rowosari'), ('Sekaran'), ('Srondol'), ('Tambakaji'), ('Tlogosari Kulon'),
('Tlogosari Wetan'), ('Bulusan'), ('Jatisari'), ('Muktiharjo')
ON CONFLICT (nama) DO NOTHING;
