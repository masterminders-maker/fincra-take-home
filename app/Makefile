up: # start in the background (detached)
	docker-compose -f docker-compose.yml up -d --build

up_log: # same as `up` but keeps logs streaming to the terminal
	docker-compose -f docker-compose.yml up --build

down: # tear everything down and clean up volumes
	docker-compose -f docker-compose.yml down --volumes

.PHONY: up up_log down