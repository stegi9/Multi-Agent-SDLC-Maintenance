-- Utenti demo per i tre ruoli RBAC. Password per tutti: password123
USE sdlc_platform;

INSERT INTO users (username, email, password_hash, domain)
VALUES ('pm', 'pm@sdlc.local', '$2b$12$P.IRuCfOltOUxSmROApjH.ermyIINzXtOodw3LvykTykZcHaugINq', NULL),
       ('sm', 'sm@sdlc.local', '$2b$12$P.IRuCfOltOUxSmROApjH.ermyIINzXtOodw3LvykTykZcHaugINq', NULL),
       ('dev', 'dev@sdlc.local', '$2b$12$P.IRuCfOltOUxSmROApjH.ermyIINzXtOodw3LvykTykZcHaugINq', 'billing')
ON DUPLICATE KEY UPDATE email = VALUES(email);

INSERT IGNORE INTO user_roles (user_id, role_id)
SELECT u.id, r.id FROM users u JOIN roles r ON r.name = 'product_manager' WHERE u.username = 'pm';

INSERT IGNORE INTO user_roles (user_id, role_id)
SELECT u.id, r.id FROM users u JOIN roles r ON r.name = 'scrum_master' WHERE u.username = 'sm';

INSERT IGNORE INTO user_roles (user_id, role_id)
SELECT u.id, r.id FROM users u JOIN roles r ON r.name = 'developer' WHERE u.username = 'dev';
