-- Wline (025): el panel de control del agente vive en Nea.
--
-- La configuración se guarda como VERSIONES INMUTABLES con un puntero a la
-- vigente: publicar inserta una versión y, solo cuando Wline aplicó sus
-- reglas, mueve el puntero con un UPDATE. Las filas viejas son el historial.
-- Las plataformas del modelo y sus llaves (cifradas, AES-256-GCM con
-- NEA_ENCRYPTION_KEY) viven aparte: no cambian con cada publicación.
--
-- `organization_id` es el id de la organización de Wline y va primero en cada
-- llave: ninguna consulta puede leer la configuración de otra sin nombrarla.
-- Idempotente como el resto: cada arranque re-aplica todas las migraciones.

CREATE TABLE IF NOT EXISTS agente_version (
  organization_id text NOT NULL,
  -- Creciente por organización y nunca reutilizado: max(local, appliedVersion
  -- de Wline) + 1. Wline es idempotente por este número.
  numero integer NOT NULL,
  contenido jsonb NOT NULL,
  resumen text NOT NULL,
  autor_id text NOT NULL,
  autor_correo text NOT NULL,
  restaurada_de integer,
  creada_en timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, numero)
);

-- Una versión sin puntero es "pendiente": si la publicación falla, se borra.
CREATE TABLE IF NOT EXISTS agente_vigente (
  organization_id text NOT NULL PRIMARY KEY,
  numero integer NOT NULL,
  actualizada_en timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (organization_id, numero) REFERENCES agente_version (organization_id, numero)
);

CREATE TABLE IF NOT EXISTS plataforma_modelo (
  organization_id text NOT NULL,
  id text NOT NULL,
  nombre text NOT NULL,
  base_url text NOT NULL,
  -- nonce (12 bytes) ‖ cifrado + tag. La llave en claro no se guarda ni sale.
  llave_cifrada bytea NOT NULL,
  llave_ultimos4 text NOT NULL,
  ultimo_exito_en timestamptz,
  ultimo_error_en timestamptz,
  ultimo_error text,
  creada_en timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, id),
  UNIQUE (organization_id, nombre)
);

-- Un eslabón está certificado si existe su fila con `aprobada` y los mismos
-- `extras`. `requiere_atencion`: la plataforma respondió con otro modelo.
CREATE TABLE IF NOT EXISTS certificacion (
  organization_id text NOT NULL,
  plataforma_id text NOT NULL,
  modelo text NOT NULL,
  extras jsonb NOT NULL DEFAULT '{}',
  lab_run_id text,
  puntaje numeric,
  aprobada boolean NOT NULL DEFAULT false,
  modelo_respondio text,
  requiere_atencion boolean NOT NULL DEFAULT false,
  certificada_en timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, plataforma_id, modelo),
  FOREIGN KEY (organization_id, plataforma_id)
    REFERENCES plataforma_modelo (organization_id, id) ON DELETE CASCADE
);

-- El eslabón que se está certificando. Solo lo usan los turnos de prueba
-- (isTest) y solo hasta `vence_en`.
CREATE TABLE IF NOT EXISTS cadena_prueba (
  organization_id text NOT NULL PRIMARY KEY,
  eslabon jsonb NOT NULL,
  lab_run_id text,
  vence_en timestamptz NOT NULL
);
