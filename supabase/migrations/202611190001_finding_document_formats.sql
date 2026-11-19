-- Keep the shared findings bucket private and allow common office documents.
-- The app also applies these settings via Storage API before a document upload,
-- so this migration records the same desired configuration for database setup.
update storage.buckets
set public = false,
    file_size_limit = 10485760,
    allowed_mime_types = array[
        'image/jpeg', 'image/png', 'image/webp',
        'application/pdf',
        'application/msword',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/vnd.ms-excel',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/vnd.ms-powerpoint',
        'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'application/vnd.oasis.opendocument.text',
        'application/vnd.oasis.opendocument.spreadsheet',
        'application/vnd.oasis.opendocument.presentation',
        'application/rtf', 'text/plain', 'text/csv'
    ]
where id = 'maintenance-finding-photos';
