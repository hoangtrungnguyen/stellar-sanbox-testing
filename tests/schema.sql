-- Minimal grava-compatible schema for e2e testing grava_plane_sync.
-- Copied from /Users/trungnguyenhoang/IdeaProjects/grava/.grava/dolt at v1.82.0.

CREATE TABLE `issues` (
  `id` varchar(32) NOT NULL,
  `title` varchar(255) NOT NULL,
  `description` longtext,
  `status` varchar(32) NOT NULL DEFAULT 'open',
  `priority` int NOT NULL DEFAULT '4',
  `issue_type` varchar(32) NOT NULL DEFAULT 'task',
  `assignee` varchar(128),
  `metadata` json,
  `created_at` timestamp DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  CONSTRAINT `check_priority` CHECK ((`priority` BETWEEN 0 AND 4)),
  CONSTRAINT `check_status` CHECK ((`status` IN ('open','in_progress','blocked','closed','tombstone','deferred','pinned','archived')))
);

CREATE TABLE `issue_labels` (
  `id` int NOT NULL AUTO_INCREMENT,
  `issue_id` varchar(32) NOT NULL,
  `label` varchar(128) NOT NULL,
  `created_at` timestamp DEFAULT CURRENT_TIMESTAMP,
  `created_by` varchar(128),
  PRIMARY KEY (`id`),
  KEY `idx_issue_labels_issue_id` (`issue_id`),
  UNIQUE KEY `unique_issue_label` (`issue_id`,`label`),
  CONSTRAINT `issue_labels_ibfk_1` FOREIGN KEY (`issue_id`) REFERENCES `issues` (`id`) ON DELETE CASCADE
);

CREATE TABLE `issue_comments` (
  `id` int NOT NULL AUTO_INCREMENT,
  `issue_id` varchar(32) NOT NULL,
  `message` text NOT NULL,
  `actor` varchar(128),
  `agent_model` varchar(256),
  `created_at` timestamp DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_issue_comments_issue_id` (`issue_id`),
  CONSTRAINT `issue_comments_ibfk_1` FOREIGN KEY (`issue_id`) REFERENCES `issues` (`id`) ON DELETE CASCADE
);

CREATE TABLE `dependencies` (
  `from_id` varchar(32) NOT NULL,
  `to_id` varchar(32) NOT NULL,
  `type` varchar(32) NOT NULL,
  `metadata` json,
  `created_by` varchar(128) DEFAULT 'unknown',
  `updated_by` varchar(128) DEFAULT 'unknown',
  `agent_model` varchar(128),
  PRIMARY KEY (`from_id`,`to_id`,`type`),
  KEY `idx_to_id` (`to_id`),
  CONSTRAINT `dependencies_ibfk_1` FOREIGN KEY (`from_id`) REFERENCES `issues` (`id`) ON DELETE CASCADE,
  CONSTRAINT `dependencies_ibfk_2` FOREIGN KEY (`to_id`) REFERENCES `issues` (`id`) ON DELETE CASCADE
);
