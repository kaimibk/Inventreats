docker-compose up --build -d

docker-compose run app /venv/bin/python manage.py load_initial_data

docker-compose up