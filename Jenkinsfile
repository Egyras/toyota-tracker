pipeline {
    agent any

    environment {
        IMAGE = 'vaikis/toyota-tracker'
        TAG   = 'latest'
    }

    // Polls Git repo every minute for new commits
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

        stage('Deploy on TrueNAS') {
            steps {
                sh """
                    # Pull the freshly built image
                    docker pull ${IMAGE}:${TAG}

                    # Stop and remove the old container (ignore errors if not running)
                    docker stop toyota-tracker || true
                    docker rm   toyota-tracker || true

                    # Start new container with updated image
                    docker run -d \
                        --name    toyota-tracker \
                        --restart unless-stopped \
                        -p        8889:8080 \
                        -e        DB_PATH=/data/stats.db \
                        -v        toyota-tracker-data:/data \
                        ${IMAGE}:${TAG}

                    echo "Deployed ${IMAGE}:${TAG} as build #${env.BUILD_NUMBER}"
                """
            }
        }
    }

    post {
        always {
            // Remove dangling images to keep TrueNAS disk clean
            sh 'docker image prune -f --filter "dangling=true" || true'
        }
        success {
            echo "✅ Build #${env.BUILD_NUMBER} deployed — http://192.168.8.132:8889"
        }
        failure {
            echo "❌ Build #${env.BUILD_NUMBER} failed"
        }
    }
}
