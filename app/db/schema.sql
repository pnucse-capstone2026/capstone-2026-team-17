-- EasyDep reset-and-recreate schema (MySQL 8.4)
-- The project intentionally rebuilds an empty database for incompatible changes.

CREATE DATABASE IF NOT EXISTS easydep
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_0900_ai_ci;

USE easydep;

CREATE TABLE IF NOT EXISTS apps (
  app_id                     VARCHAR(36) NOT NULL,
  requirements_text          MEDIUMTEXT NULL,
  resource_constraints_text  MEDIUMTEXT NULL,
  current_stage              VARCHAR(32) NULL,
  deployment_preferences     JSON NULL,
  requirements_gated         TINYINT(1) NULL,
  created_at                 DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (app_id),
  KEY ix_apps_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS artifact_versions (
  id             BIGINT      NOT NULL AUTO_INCREMENT,
  app_id         VARCHAR(36) NOT NULL,
  artifact_type  VARCHAR(32) NOT NULL,
  version_no     INT         NOT NULL,
  content        LONGTEXT    NOT NULL,
  syntax_valid   TINYINT(1)  NULL,
  syntax_errors  JSON        NULL,
  origin         VARCHAR(20) NOT NULL DEFAULT 'GENERATED',
  created_at     DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_artifact_versions_app_type_no
    (app_id, artifact_type, version_no),
  CONSTRAINT ck_versions_version_positive CHECK (version_no > 0),
  CONSTRAINT fk_artifact_versions_app FOREIGN KEY (app_id)
    REFERENCES apps (app_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS artifact_files (
  artifact_version_id BIGINT NOT NULL,
  file_path VARCHAR(512) CHARACTER SET utf8mb4
    COLLATE utf8mb4_0900_as_cs NOT NULL,
  content LONGTEXT NOT NULL,
  sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  PRIMARY KEY (artifact_version_id, file_path),
  CONSTRAINT ck_artifact_files_path CHECK (CHAR_LENGTH(file_path) > 0),
  CONSTRAINT ck_artifact_files_sha256 CHECK (sha256 REGEXP '^[0-9a-f]{64}$'),
  CONSTRAINT fk_artifact_files_version FOREIGN KEY (artifact_version_id)
    REFERENCES artifact_versions (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS workspace_commands (
  command_id   VARCHAR(36) NOT NULL,
  app_id       VARCHAR(36) NOT NULL,
  action       VARCHAR(48) NOT NULL,
  stage        VARCHAR(32) NOT NULL,
  status       VARCHAR(24) NOT NULL,
  payload      JSON        NOT NULL,
  result       JSON        NULL,
  error        LONGTEXT    NULL,
  created_at   DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  started_at   DATETIME(6) NULL,
  completed_at DATETIME(6) NULL,
  PRIMARY KEY (command_id),
  KEY ix_workspace_commands_app_created (app_id, created_at),
  KEY ix_workspace_commands_app_status (app_id, status),
  KEY ix_workspace_commands_status (status),
  CONSTRAINT fk_workspace_commands_app FOREIGN KEY (app_id)
    REFERENCES apps (app_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_checkpoints (
  graph_type            VARCHAR(16)  NOT NULL,
  thread_id             VARCHAR(128) NOT NULL,
  checkpoint_ns         VARCHAR(128) NOT NULL DEFAULT '',
  checkpoint_id         VARCHAR(128) NOT NULL,
  parent_checkpoint_id  VARCHAR(128) NULL,
  checkpoint_type       VARCHAR(32)  NOT NULL,
  checkpoint            LONGBLOB     NOT NULL,
  metadata_type         VARCHAR(32)  NOT NULL,
  checkpoint_metadata   LONGBLOB     NOT NULL,
  PRIMARY KEY (graph_type, thread_id, checkpoint_ns, checkpoint_id),
  KEY ix_agent_checkpoints_graph_checkpoint (graph_type, checkpoint_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_checkpoint_blobs (
  graph_type    VARCHAR(16)  NOT NULL,
  thread_id     VARCHAR(128) NOT NULL,
  checkpoint_ns VARCHAR(128) NOT NULL DEFAULT '',
  channel       VARCHAR(255) NOT NULL,
  version       VARCHAR(64)  NOT NULL,
  blob_type     VARCHAR(32)  NOT NULL,
  blob          LONGBLOB     NULL,
  PRIMARY KEY (graph_type, thread_id, checkpoint_ns, channel, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_checkpoint_writes (
  graph_type    VARCHAR(16)  NOT NULL,
  thread_id     VARCHAR(128) NOT NULL,
  checkpoint_ns VARCHAR(128) NOT NULL DEFAULT '',
  checkpoint_id VARCHAR(128) NOT NULL,
  task_id       VARCHAR(128) NOT NULL,
  idx           INT          NOT NULL,
  channel       VARCHAR(255) NOT NULL,
  write_type    VARCHAR(32)  NOT NULL,
  blob          LONGBLOB     NOT NULL,
  task_path     VARCHAR(255) NOT NULL DEFAULT '',
  PRIMARY KEY (graph_type, thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
