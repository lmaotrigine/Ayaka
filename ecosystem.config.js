module.exports = {
    apps: [
        {
            name: 'Ayaka',
            cwd: '/home/frodo/bots/ayaka/',
            script: 'launcher.py',
            autorestart: true,
            max_restart: 5,
            instances: 1,
            env: {
                JISHAKU_HIDE: 'True',
                JISHAKU_RETAIN: 'True',
                JISHAKU_NO_UNDERSCORE: 'True',
                JISHAKU_NO_DM_TRACEBACK: 'True'
            },
            interpreter: 'poetry',
            interpreter_args: [
                'run',
                'python',
                '-O'
            ],
            log_date_format: 'DD-MM-YYYY HH:mm:ss'
        }
    ]
}
