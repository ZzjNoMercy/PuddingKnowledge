CREATE TABLE analytics_query_results (
	id VARCHAR(64) NOT NULL,
	session_id VARCHAR(120) NOT NULL,
	tool_call_id VARCHAR(120) NOT NULL,
	question TEXT NOT NULL,
	sql TEXT NOT NULL,
	columns JSON NOT NULL,
	row_count INTEGER NOT NULL,
	profile_json JSON NOT NULL,
	artifact_path TEXT NOT NULL,
	artifact_format VARCHAR(20) NOT NULL,
	status VARCHAR(40) NOT NULL,
	created_at DATETIME NOT NULL,
	expires_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE feishu_app_credentials (
	id VARCHAR(64) NOT NULL,
	owner_id VARCHAR(120) NOT NULL,
	app_id_masked VARCHAR(120) NOT NULL,
	credential_ref TEXT NOT NULL,
	api_base_url VARCHAR(300) NOT NULL,
	app_name VARCHAR(200) NOT NULL,
	tenant_key VARCHAR(200) NOT NULL,
	status VARCHAR(40) NOT NULL,
	validated_at DATETIME,
	rotated_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE feishu_oauth_sessions (
	id VARCHAR(64) NOT NULL,
	state_hash VARCHAR(64) NOT NULL,
	app_credential_id VARCHAR(64) NOT NULL,
	source_connection_id VARCHAR(64) NOT NULL,
	principal_id VARCHAR(120) NOT NULL,
	redirect_uri TEXT NOT NULL,
	verifier_credential_ref TEXT NOT NULL,
	requested_scopes JSON NOT NULL,
	status VARCHAR(40) NOT NULL,
	expires_at DATETIME NOT NULL,
	consumed_at DATETIME,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_feishu_oauth_session_state_hash UNIQUE (state_hash),
	FOREIGN KEY(app_credential_id) REFERENCES feishu_app_credentials (id),
	FOREIGN KEY(source_connection_id) REFERENCES knowledge_source_connections (id)
);
CREATE TABLE feishu_user_grants (
	id VARCHAR(64) NOT NULL,
	app_credential_id VARCHAR(64) NOT NULL,
	source_connection_id VARCHAR(64),
	principal_id VARCHAR(120) NOT NULL,
	open_id VARCHAR(200) NOT NULL,
	union_id VARCHAR(200) NOT NULL,
	tenant_key VARCHAR(200) NOT NULL,
	token_credential_ref TEXT NOT NULL,
	granted_scopes JSON NOT NULL,
	access_expires_at DATETIME,
	refresh_expires_at DATETIME,
	token_version INTEGER NOT NULL,
	status VARCHAR(40) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_feishu_user_grant_binding UNIQUE (app_credential_id, source_connection_id, principal_id),
	FOREIGN KEY(app_credential_id) REFERENCES feishu_app_credentials (id),
	FOREIGN KEY(source_connection_id) REFERENCES knowledge_source_connections (id)
);
CREATE TABLE knowledge_bases (
	id VARCHAR(64) NOT NULL,
	name VARCHAR(200) NOT NULL,
	description TEXT NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE knowledge_database_sources (
	id VARCHAR(64) NOT NULL,
	knowledge_base_id VARCHAR(64) NOT NULL,
	source_type VARCHAR(40) NOT NULL,
	name VARCHAR(200) NOT NULL,
	description TEXT NOT NULL,
	host VARCHAR(300) NOT NULL,
	port INTEGER NOT NULL,
	"database" VARCHAR(200) NOT NULL,
	username VARCHAR(200) NOT NULL,
	password TEXT NOT NULL,
	selected_tables JSON NOT NULL,
	source_metadata JSON NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_kb_database_source_name UNIQUE (knowledge_base_id, name),
	FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases (id)
);
CREATE TABLE knowledge_documents (
	id VARCHAR(64) NOT NULL,
	knowledge_base_id VARCHAR(64) NOT NULL,
	title VARCHAR(300) NOT NULL,
	source_type VARCHAR(40) NOT NULL,
	source_path TEXT NOT NULL,
	storage_path TEXT NOT NULL,
	virtual_path TEXT NOT NULL,
	mime_type VARCHAR(120) NOT NULL,
	content_sha256 VARCHAR(64) NOT NULL,
	size_bytes INTEGER NOT NULL,
	status VARCHAR(40) NOT NULL,
	publish_targets JSON NOT NULL,
	doc_metadata JSON NOT NULL,
	source_connection_id VARCHAR(64),
	source_item_id VARCHAR(64),
	origin_url TEXT,
	source_revision VARCHAR(200),
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases (id),
	FOREIGN KEY(source_connection_id) REFERENCES knowledge_source_connections (id)
);
CREATE TABLE knowledge_import_events (
	id VARCHAR(64) NOT NULL,
	job_id VARCHAR(64) NOT NULL,
	level VARCHAR(20) NOT NULL,
	message TEXT NOT NULL,
	event_metadata JSON NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(job_id) REFERENCES knowledge_import_jobs (id)
);
CREATE TABLE knowledge_import_jobs (
	id VARCHAR(64) NOT NULL,
	knowledge_base_id VARCHAR(64) NOT NULL,
	status VARCHAR(40) NOT NULL,
	file_name VARCHAR(500) NOT NULL,
	file_type VARCHAR(40) NOT NULL,
	file_size INTEGER NOT NULL,
	source_path TEXT NOT NULL,
	source_sha256 VARCHAR(64) NOT NULL,
	title VARCHAR(300),
	publish_targets JSON NOT NULL,
	current_step VARCHAR(80) NOT NULL,
	progress INTEGER NOT NULL,
	document_id VARCHAR(64),
	source_connection_id VARCHAR(64),
	source_item_id VARCHAR(64),
	sync_run_id VARCHAR(64),
	error_message TEXT,
	retry_count INTEGER NOT NULL,
	job_metadata JSON NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	started_at DATETIME,
	finished_at DATETIME,
	lease_owner VARCHAR(120),
	lease_expires_at DATETIME,
	heartbeat_at DATETIME,
	attempt INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases (id),
	FOREIGN KEY(document_id) REFERENCES knowledge_documents (id),
	FOREIGN KEY(source_connection_id) REFERENCES knowledge_source_connections (id),
	FOREIGN KEY(source_item_id) REFERENCES knowledge_source_items (id),
	FOREIGN KEY(sync_run_id) REFERENCES knowledge_sync_runs (id)
);
CREATE TABLE knowledge_source_connections (
	id VARCHAR(64) NOT NULL,
	knowledge_base_id VARCHAR(64) NOT NULL,
	connector_key VARCHAR(80) NOT NULL,
	name VARCHAR(200) NOT NULL,
	status VARCHAR(40) NOT NULL,
	auth_type VARCHAR(40) NOT NULL,
	credential_ref TEXT NOT NULL,
	config_json JSON NOT NULL,
	schedule_json JSON NOT NULL,
	last_sync_run_id VARCHAR(64),
	last_synced_at DATETIME,
	last_error_json JSON NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases (id)
);
CREATE TABLE knowledge_source_items (
	id VARCHAR(64) NOT NULL,
	knowledge_base_id VARCHAR(64) NOT NULL,
	source_connection_id VARCHAR(64) NOT NULL,
	external_id VARCHAR(500) NOT NULL,
	external_parent_id VARCHAR(500),
	external_type VARCHAR(80) NOT NULL,
	title VARCHAR(500) NOT NULL,
	source_url TEXT,
	path_json JSON NOT NULL,
	revision VARCHAR(200),
	content_sha256 VARCHAR(64),
	document_id VARCHAR(64),
	status VARCHAR(40) NOT NULL,
	remote_created_at DATETIME,
	remote_updated_at DATETIME,
	last_seen_run_id VARCHAR(64),
	metadata_json JSON NOT NULL,
	permissions_json JSON NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_source_item_external_id UNIQUE (source_connection_id, external_id),
	FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases (id),
	FOREIGN KEY(source_connection_id) REFERENCES knowledge_source_connections (id),
	FOREIGN KEY(document_id) REFERENCES knowledge_documents (id)
);
CREATE TABLE knowledge_sync_runs (
	id VARCHAR(64) NOT NULL,
	source_connection_id VARCHAR(64) NOT NULL,
	mode VARCHAR(40) NOT NULL,
	status VARCHAR(40) NOT NULL,
	cursor_json JSON NOT NULL,
	stats_json JSON NOT NULL,
	current_step VARCHAR(80) NOT NULL,
	progress INTEGER NOT NULL,
	error_json JSON NOT NULL,
	started_at DATETIME,
	finished_at DATETIME,
	lease_owner VARCHAR(120),
	lease_expires_at DATETIME,
	heartbeat_at DATETIME,
	attempt INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(source_connection_id) REFERENCES knowledge_source_connections (id)
);
CREATE TABLE knowledge_table_assets (
	asset_id VARCHAR(64) NOT NULL,
	knowledge_base_id VARCHAR(64) NOT NULL,
	document_id VARCHAR(64),
	source_type VARCHAR(40) NOT NULL,
	file_name VARCHAR(500) NOT NULL,
	storage_path TEXT NOT NULL,
	virtual_path TEXT NOT NULL,
	sheet_name VARCHAR(300),
	size_bytes INTEGER NOT NULL,
	modified_at DATETIME,
	content_sha256 VARCHAR(64) NOT NULL,
	profile_status VARCHAR(40) NOT NULL,
	profile_path TEXT NOT NULL,
	rows INTEGER,
	columns_count INTEGER,
	columns JSON NOT NULL,
	reference_status VARCHAR(40) NOT NULL,
	asset_metadata JSON NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (asset_id),
	CONSTRAINT uq_kb_table_asset_virtual_sheet UNIQUE (knowledge_base_id, virtual_path, sheet_name),
	FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases (id),
	FOREIGN KEY(document_id) REFERENCES knowledge_documents (id)
);
CREATE TABLE read_later_items (
	id VARCHAR(64) NOT NULL,
	knowledge_base_id VARCHAR(64) NOT NULL,
	original_url TEXT NOT NULL,
	canonical_url TEXT NOT NULL,
	title VARCHAR(500) NOT NULL,
	site_name VARCHAR(300) NOT NULL,
	author VARCHAR(300) NOT NULL,
	description TEXT NOT NULL,
	image_url TEXT NOT NULL,
	storage_path TEXT NOT NULL,
	virtual_path TEXT NOT NULL,
	content_sha256 VARCHAR(64) NOT NULL,
	parse_status VARCHAR(40) NOT NULL,
	reading_status VARCHAR(40) NOT NULL,
	error_message TEXT NOT NULL,
	tags JSON NOT NULL,
	note TEXT NOT NULL,
	document_id VARCHAR(64),
	source_connection_id VARCHAR(64),
	source_item_id VARCHAR(64),
	raw_snapshot_path TEXT NOT NULL,
	wiki_job_id VARCHAR(64) NOT NULL,
	fetched_at DATETIME,
	read_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_read_later_kb_canonical_url UNIQUE (knowledge_base_id, canonical_url),
	FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases (id),
	FOREIGN KEY(document_id) REFERENCES knowledge_documents (id),
	FOREIGN KEY(source_connection_id) REFERENCES knowledge_source_connections (id),
	FOREIGN KEY(source_item_id) REFERENCES knowledge_source_items (id)
);
CREATE TABLE semantic_dimension_build_events (
	id VARCHAR(64) NOT NULL,
	job_id VARCHAR(64) NOT NULL,
	level VARCHAR(20) NOT NULL,
	message TEXT NOT NULL,
	event_metadata JSON NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(job_id) REFERENCES semantic_dimension_build_jobs (id)
);
CREATE TABLE semantic_dimension_build_jobs (
	id VARCHAR(64) NOT NULL,
	session_id VARCHAR(120) NOT NULL,
	query_id VARCHAR(120) NOT NULL,
	dimension_id VARCHAR(160) NOT NULL,
	adapter VARCHAR(160) NOT NULL,
	requested_scope JSON NOT NULL,
	input_snapshot JSON NOT NULL,
	status VARCHAR(60) NOT NULL,
	current_step VARCHAR(80) NOT NULL,
	progress INTEGER NOT NULL,
	staging_path TEXT NOT NULL,
	published_reference_path TEXT NOT NULL,
	result_summary JSON NOT NULL,
	error_message TEXT,
	retry_count INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	started_at DATETIME,
	finished_at DATETIME,
	lease_owner VARCHAR(120),
	lease_expires_at DATETIME,
	heartbeat_at DATETIME,
	attempt INTEGER NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE task_notifications (
	id VARCHAR(64) NOT NULL,
	category VARCHAR(80) NOT NULL,
	subject_type VARCHAR(80) NOT NULL,
	subject_id VARCHAR(64) NOT NULL,
	title VARCHAR(300) NOT NULL,
	body TEXT NOT NULL,
	payload JSON NOT NULL,
	created_at DATETIME NOT NULL,
	read_at DATETIME,
	PRIMARY KEY (id)
);
CREATE TABLE worker_access_logs (
	id VARCHAR(64) NOT NULL,
	key_id VARCHAR(120) NOT NULL,
	key_name VARCHAR(120) NOT NULL,
	"query" TEXT NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX ix_analytics_query_results_created ON analytics_query_results (created_at);
CREATE INDEX ix_analytics_query_results_expires ON analytics_query_results (expires_at);
CREATE INDEX ix_analytics_query_results_session ON analytics_query_results (session_id, created_at);
CREATE INDEX ix_feishu_app_credentials_owner ON feishu_app_credentials (owner_id, updated_at);
CREATE INDEX ix_feishu_oauth_sessions_expiry ON feishu_oauth_sessions (status, expires_at);
CREATE INDEX ix_feishu_user_grants_status ON feishu_user_grants (status, updated_at);
CREATE INDEX ix_knowledge_database_sources_kb_updated ON knowledge_database_sources (knowledge_base_id, updated_at);
CREATE INDEX ix_knowledge_documents_kb_created ON knowledge_documents (knowledge_base_id, created_at);
CREATE INDEX ix_knowledge_documents_source_connection ON knowledge_documents (source_connection_id, updated_at);
CREATE INDEX ix_knowledge_documents_source_item ON knowledge_documents (source_item_id);
CREATE INDEX ix_knowledge_import_events_job_created ON knowledge_import_events (job_id, created_at);
CREATE INDEX ix_knowledge_import_jobs_kb_created ON knowledge_import_jobs (knowledge_base_id, created_at);
CREATE INDEX ix_knowledge_import_jobs_source ON knowledge_import_jobs (source_connection_id, created_at);
CREATE INDEX ix_knowledge_import_jobs_status_created ON knowledge_import_jobs (status, created_at);
CREATE INDEX ix_knowledge_import_jobs_sync_run ON knowledge_import_jobs (sync_run_id, created_at);
CREATE INDEX ix_knowledge_source_connections_kb_updated ON knowledge_source_connections (knowledge_base_id, updated_at);
CREATE INDEX ix_knowledge_source_connections_status ON knowledge_source_connections (status, updated_at);
CREATE INDEX ix_knowledge_source_items_document ON knowledge_source_items (document_id);
CREATE INDEX ix_knowledge_source_items_last_seen ON knowledge_source_items (source_connection_id, last_seen_run_id);
CREATE INDEX ix_knowledge_source_items_source_status ON knowledge_source_items (source_connection_id, status);
CREATE INDEX ix_knowledge_sync_runs_source_created ON knowledge_sync_runs (source_connection_id, created_at);
CREATE INDEX ix_knowledge_sync_runs_status_created ON knowledge_sync_runs (status, created_at);
CREATE INDEX ix_knowledge_table_assets_kb_profile ON knowledge_table_assets (knowledge_base_id, profile_status);
CREATE INDEX ix_knowledge_table_assets_kb_updated ON knowledge_table_assets (knowledge_base_id, updated_at);
CREATE INDEX ix_read_later_kb_created ON read_later_items (knowledge_base_id, created_at);
CREATE INDEX ix_read_later_kb_status ON read_later_items (knowledge_base_id, reading_status, parse_status);
CREATE INDEX ix_read_later_source_item ON read_later_items (source_item_id);
CREATE INDEX ix_semantic_dimension_build_events_job_created ON semantic_dimension_build_events (job_id, created_at);
CREATE INDEX ix_semantic_dimension_build_jobs_dimension_created ON semantic_dimension_build_jobs (dimension_id, created_at);
CREATE INDEX ix_semantic_dimension_build_jobs_status_created ON semantic_dimension_build_jobs (status, created_at);
CREATE INDEX ix_task_notifications_subject_created ON task_notifications (subject_type, subject_id, created_at);
CREATE INDEX ix_task_notifications_unread_created ON task_notifications (read_at, created_at);
CREATE INDEX ix_worker_access_logs_created ON worker_access_logs (created_at);
CREATE INDEX ix_worker_access_logs_key_name ON worker_access_logs (key_name);
