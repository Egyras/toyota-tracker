pipeline {
    agent any

    environment {
        IMAGE        = 'vaikis/toyota-tracker'
        TAG          = 'latest'
        MST_EMAIL    = credentials('mst-email')
        MST_PASSWORD = credentials('mst-password')
        // Shared secret authenticating the web container to the scraper's
        // internal API. Create this in Jenkins first:
        //   Manage Jenkins > Credentials > add a "Secret text" with ID
        //   'scraper-token', value: output of `openssl rand -hex 32`
        SCRAPER_TOKEN = credentials('scraper-token')
    }

    triggers {
        pollSCM('* * * * *')
    }

    stages {

        stage('Checkout') {
            steps {
                checkout scm
                echo "Building commit ${env.GIT_COMMIT?.take(8)} on ${env.GIT_BRANCH}"
            }
        }

        stage('Build image') {
            steps {
                sh """
                    docker build \
                        -t ${IMAGE}:${TAG} \
                        -t ${IMAGE}:build-${env.BUILD_NUMBER} \
                        .
                """
            }
        }

        stage('Push to Docker Hub') {
            steps {
                withCredentials([usernamePassword(
                    credentialsId: 'dockerhub',
                    usernameVariable: 'DOCKER_USER',
                    passwordVariable: 'DOCKER_PASS'
                )]) {
                    sh """
                        echo "\$DOCKER_PASS" | docker login -u "\$DOCKER_USER" --password-stdin
                        docker push ${IMAGE}:${TAG}
                        docker push ${IMAGE}:build-${env.BUILD_NUMBER}
                        docker logout
                    """
                }
            }
        }

        // NOTE ON SECRETS: `-e MST_PASSWORD` is passed WITHOUT a value on purpose.
        // Docker then copies it from Jenkins' own environment. Writing
        // -e MST_PASSWORD="${MST_PASSWORD}" made Groovy interpolate the secret
        // into the shell string, which put it in `docker run`'s argv — readable
        // via `ps` by any user on the TrueNAS host. Jenkins also warns about
        // Groovy-interpolated secrets in sh steps for the same reason.
        //
        // CHROMIUM_NO_SANDBOX=1 preserves today's behaviour. The image now runs
        // the browser as the unprivileged `pwuser` rather than root, so a
        // renderer exploit no longer lands as root — but Chromium's own sandbox
        // still needs Docker's seccomp profile relaxed to create user
        // namespaces. To finish the job:
        //   1. ./install-seccomp-profile.sh          (once, on the host)
        //   2. add --security-opt seccomp=/etc/docker/seccomp/playwright.json
        //   3. delete the -e CHROMIUM_NO_SANDBOX=1 line
        //   4. redeploy and confirm vessel detection still works
        stage('Deploy on TrueNAS') {
            steps {
                withCredentials([usernamePassword(
                    credentialsId: 'dockerhub',
                    usernameVariable: 'DOCKER_USER',
                    passwordVariable: 'DOCKER_PASS'
                )]) {
                sh """
                    set -eu
                    echo "\$DOCKER_PASS" | docker login -u "\$DOCKER_USER" --password-stdin
                    docker pull ${IMAGE}:${TAG}

                    docker stop toyota-tracker toyota-scraper 2>/dev/null || true
                    docker rm   toyota-tracker toyota-scraper 2>/dev/null || true

                    OLDPORT=\$(docker ps -q --filter publish=8889)
                    if [ -n "\$OLDPORT" ]; then
                        docker stop \$OLDPORT || true
                        docker rm   \$OLDPORT || true
                    fi

                    # ── Networks ──────────────────────────────────────────────
                    # toyota-internal is --internal: no gateway, so the scraper
                    # cannot route anywhere except the web container. This is the
                    # ONLY network the scraper shares with the web app.
                    docker network inspect toyota-internal >/dev/null 2>&1 || \
                        docker network create --internal toyota-internal

                    # toyota-egress carries the scraper's outbound HTTPS to
                    # myshiptracking.com. Fixed subnet so the host firewall rules
                    # in install-lan-isolation.sh have a stable source range to
                    # match on. Docker alone cannot say "internet yes, LAN no" —
                    # that script is what actually blocks 192.168/10/172.16.
                    docker network inspect toyota-egress >/dev/null 2>&1 || \
                        docker network create --subnet 172.31.77.0/24 toyota-egress

                    # ── Scraper ───────────────────────────────────────────────
                    # Holds the MyShipTracking login and nothing else: no volume,
                    # no DB_PATH, no published port, no Toyota credentials.
                    #
                    # --user pwuser: the container is unprivileged from PID 1, so
                    # Chromium can sandbox and nothing needs to setuid. An earlier
                    # version ran as root and had web.py drop privileges per
                    # subprocess — which fails with EPERM under --cap-drop=ALL,
                    # because dropping to another user needs CAP_SETUID/SETGID.
                    # Starting unprivileged avoids needing the capability at all.
                    #
                    # tmpfs needs mode=1777: Docker's default is root-owned, and
                    # pwuser must be able to write both /tmp and its HOME or
                    # Chromium will not start. /tmp is deliberately NOT noexec —
                    # with --disable-dev-shm-usage Chromium maps shared memory
                    # there, and noexec breaks it in ways that are hard to read
                    # from the logs.
                    docker run -d \
                        --name    toyota-scraper \
                        --restart unless-stopped \
                        --init \
                        --ipc=host \
                        --network toyota-egress \
                        --user    pwuser \
                        --cap-drop=ALL \
                        --security-opt no-new-privileges \
                        --pids-limit 512 \
                        --memory 2g \
                        --read-only \
                        --tmpfs /tmp:rw,nosuid,size=512m,mode=1777 \
                        --tmpfs /home/pwuser:rw,nosuid,size=256m,mode=1777 \
                        -e        ROLE=scraper \
                        -e        MST_EMAIL \
                        -e        MST_PASSWORD \
                        -e        SCRAPER_TOKEN \
                        -e        CHROMIUM_NO_SANDBOX=1 \
                        ${IMAGE}:${TAG}

                    docker network connect toyota-internal toyota-scraper

                    # ── Web ───────────────────────────────────────────────────
                    # Keeps the database and users' Toyota credentials. Note the
                    # absence of MST_EMAIL/MST_PASSWORD: it no longer runs a
                    # browser, so it has no use for them.
                    # Stays on the DEFAULT bridge with -p exactly as before:
                    # published ports do not work on an --internal network, and
                    # the Cloudflare tunnel has to be able to reach this.
                    # toyota-internal is then attached as a second interface.
                    docker run -d \
                        --name    toyota-tracker \
                        --restart unless-stopped \
                        --init \
                        --cap-drop=ALL \
                        --security-opt no-new-privileges \
                        --pids-limit 256 \
                        --memory 1g \
                        -p        8889:8080 \
                        -e        DB_PATH=/data/stats.db \
                        -e        ROLE=web \
                        -e        SCRAPER_URL=http://toyota-scraper:8080 \
                        -e        SCRAPER_TOKEN \
                        -v        toyota-tracker-data:/data \
                        ${IMAGE}:${TAG}

                    docker network connect toyota-internal toyota-tracker

                    # Fail the build if the two cannot talk — otherwise vessel
                    # detection silently returns nothing and looks like a data bug.
                    sleep 5
                    docker exec toyota-tracker python -c "
import os,sys,urllib.request
try:
    r=urllib.request.urlopen(os.environ['SCRAPER_URL']+'/healthz',timeout=10)
    print('scraper healthz:', r.read().decode())
except Exception as e:
    sys.exit('FATAL: web container cannot reach scraper: %s' % e)
"

                    echo "Deployed ${IMAGE}:${TAG} as build #${env.BUILD_NUMBER}"
                """
                }
            }
        }
    }

    post {
        always {
            sh 'docker image prune -f --filter "dangling=true" || true'
        }
        success {
            // Clean up old tags on Docker Hub — keep only 'latest' and current build.
            withCredentials([usernamePassword(
                credentialsId: 'dockerhub',
                usernameVariable: 'DOCKER_USER',
                passwordVariable: 'DOCKER_PASS'
            )]) {
                sh """
                    set +e
                    REPO="${IMAGE}"
                    KEEP_BUILD="build-${env.BUILD_NUMBER}"
                    echo "Cleaning Docker Hub — keeping: latest, \$KEEP_BUILD"

                    TOKEN=\$(curl -sf "https://hub.docker.com/v2/users/login/" \
                        -H "Content-Type: application/json" \
                        -d '{"username": "'\$DOCKER_USER'", "password": "'\$DOCKER_PASS'"}' \
                        | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])" 2>/dev/null)

                    if [ -z "\$TOKEN" ]; then echo "Hub login failed, skipping cleanup"; exit 0; fi

                    curl -sf "https://hub.docker.com/v2/repositories/\$REPO/tags/?page_size=100" \
                        -H "Authorization: Bearer \$TOKEN" \
                        | python3 -c "import sys,json;[print(t['name']) for t in json.load(sys.stdin).get('results',[])]" \
                        | while read tag; do
                            if [ "\$tag" = "latest" ] || [ "\$tag" = "\$KEEP_BUILD" ]; then
                                echo "  keeping: \$tag"
                            else
                                echo "  deleting: \$tag"
                                curl -sf -X DELETE \
                                    "https://hub.docker.com/v2/repositories/\$REPO/tags/\$tag/" \
                                    -H "Authorization: Bearer \$TOKEN" || true
                            fi
                        done
                    echo "Hub cleanup done"
                """
            }
            echo "✅ Build #${env.BUILD_NUMBER} deployed — http://192.168.8.211:8889"
        }
        failure {
            echo "❌ Build #${env.BUILD_NUMBER} failed"
        }
    }
}