import asyncio
import signal
import functools
import datetime
import argparse
import os
from slugify import slugify
from .reloader import watchdog
from .player import Player
from .controller import Controller
from . import db, cursor

WELCOME_MSG = fr"""
{cursor.CLEAR}{cursor.GREEN}

   _____                   _   _ _       _       
  |_   _|                 | \ | (_)     (_)      
    | | ___ _ __ _ __ ___ |  \| |_ _ __  _  __ _ 
    | |/ _ \ '__| '_ ` _ \| . ` | | '_ \| |/ _` |
    | |  __/ |  | | | | | | |\  | | | | | | (_| |
    \_/\___|_|  |_| |_| |_\_| \_/_|_| |_| |\__,_|
                                        / |      
                                       |__/

{cursor.RESET}
"""

parser = argparse.ArgumentParser()
parser.add_argument('-p', '--port', action='store',
                    type=int, dest='port', default=3000)
parser.add_argument('-a', '--auto-reload', action='store_true',
                    dest='auto_reload')

class Server:
    HOST = "0.0.0.0"
    welcome_message = WELCOME_MSG
    continuation_message = cursor.blue("Press enter to get started...")
    controller_class = Controller
    player_count = 1
    description = ''
    
    def start(self):
        """
        Run dis
        """
        args = parser.parse_args()
        self.port = args.port

        if (args.auto_reload and 
            os.environ.get('TERMNINJA_SERVER_RUNNING') != "true"
        ):
            watchdog(2)
        else:
            asyncio.run(self._start_serving(), debug=True)
    
    def get_controller_class(self):
        """
        can override this to have dynamic controllers
        """
        return self.controller_class
    
    def get_server_name(self):
        """
        This is the SERVER friendly name (used as a key in database)
        """
        return getattr(self, 'friendly_name', self.__class__.__name__)

    async def on_server_started(self):
        """
        Hook called after the server is initialized
        """
        pass

    async def on_player_connected(self, player):
        """
        Hook called when a player first connects
        """
        await player.send(self.welcome_message)

    async def should_accept_player(self, player):
        """
        Hook called to determine if a player should be allowed to play
        """
        return True
    
    async def on_player_accepted(self, player):
        """
        Hook called when a player is determined allowed to play
        """
        await player.send(self.continuation_message)
        await player.readline()
    
    async def on_player_rejected(self, player):
        """
        Read/Write opportunity after a player is rejected.
        Do NOT close stream here
        """
        pass

    async def _start_serving(self):
        """
        1. connect to the database
        2. start _self._server_task()
            producer of players
        3. start _self.start_game_task()
            consumer of players to start controller to handle connected players 
        4. run until complete/cancelled
        5. disconnect from the database
        """
        await self._register_signal_handlers()
        await self._initialize()
        try:
            server_task = asyncio.create_task(
                self._server_task()
            )
            start_games_task = asyncio.create_task(
                self._start_game_task()
            )
            await self.on_server_started()
            await asyncio.gather(
                server_task,
                start_games_task
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self._teardown()
    
    async def _register_signal_handlers(self):
        """
        Register handlers in the event loop for stop signals
        """
        loop = asyncio.get_running_loop()
        for signame in {'SIGINT', 'SIGTERM'}:
            loop.add_signal_handler(
                getattr(signal, signame),
                functools.partial(self._handle_stop_signal, signame, loop)
            )

    def _handle_stop_signal(self, signal, loop):
        """
        Kill dis
        """
        print(f"[!] Recieved {signal}")
        for task in asyncio.Task.all_tasks():
            task.cancel()
    
    async def _initialize(self):
        """
        initialize environment for the server
        """
        await db.conn.connect()
        self._player_queue = asyncio.Queue()

    async def _teardown(self):
        """
        teardown any resources, etc.
        """
        await db.conn.disconnect()

    async def _server_task(self):
        """
        Start the async server.
        """
        server = await asyncio.start_server(
            self._on_connection,
            host=self.HOST,
            port=self.port,
            reuse_port=True
        )
        async with server:
            await server.serve_forever()

    async def _on_connection(self, reader, writer):
        """
        Queue a newly connected player after calling appropriate hooks.
        """
        player = Player(reader, writer)
        print('[+] connection: ', player.address)
        try:
            await self.on_player_connected(player)
            await self._accept_player(player)
        except ConnectionResetError:
            await player.close()

    async def _accept_player(self, player):
        """
        Determine if a player should be accepted, call appropriate hooks,
        add player to the _player_queue
        """
        if await self.should_accept_player(player):
            await self.on_player_accepted(player)
            await self._player_queue.put(player)
        else:
            await self.on_player_rejected(player)
            await player.close() 

    async def _start_game_task(self):
        """
        Block until self.player_count players have connected, then call run.
        """
        while True:
            asyncio.create_task(self._launch_game(*[
                await self._player_queue.get() for _ in range(self.player_count)
            ]))

    async def _launch_game(self, *players):
        """
        create an instance of game_controller and call it's run
        method
        """
        controller_class = self.get_controller_class()
        controller = controller_class(*players, server_friendly_name=self.get_friendly_name())
        asyncio.create_task(controller.start()) 


class OptionalAuthenticationMixin:
    """ 
    Allow a player to authenticate if they want to update score, 
    otherwise play anonymously
    """
    enter_token_prompt = "Enter a token to track score or press enter to play anonymously: "
    erase_input = (
        f"{cursor.up(1)}"
        f"{cursor.move_to_column(len(enter_token_prompt))}"
        f"{cursor.ERASE_TO_LINE_END}"
    )
    token_accepted_message = f"{erase_input}{cursor.green(' accepted')}\n"
    token_rejected_message = f"{erase_input}{cursor.red(' rejected')}\n"
    token_expired_message = f"{erase_input}{cursor.red(' token expired')}\n"

    def token_is_expired(self, expiration_datetime):
        return expiration_datetime < datetime.datetime.now()

    async def should_accept_player(self, player):
        await player.send(self.enter_token_prompt)
        token = await player.readline()
        if token == "":
            # play anonymously
            return True
        user = await db.users.select_by_play_token(token)
        if user is None:
            # invalid token
            await player.send(self.token_rejected_message)
            return False
        if self.token_is_expired(user['play_token_expires_at']):
            # expired token
            await player.send(self.token_expired_message)
            return False
        # accepted token
        await player.send(self.token_accepted_message)
        player.assign_db_user(user)
        return True
    
    async def on_player_accepted(self, player):
        await player.send(
            f"\n"
            f"username:         {cursor.green(player.username)}\n"
            f"score:            {cursor.green(player.score)}\n"
            f"token expires in: {cursor.green(player.play_token_expires_at)}\n\n"
        )
        return await super().on_player_accepted(player)


class PingDatabaseMixin:
    ping_database_interval = 2 * 60 # every 2 minutes

    async def on_server_started(self):
        asyncio.create_task(self._update_database_task())
        return await super().on_server_started()
    
    async def _update_database_task(self):
        server_name = self.get_server_name()
        slug = slugify(server_name)
        await db.games.create_or_update_game(slug, {
            'server_name': server_name,
            'description': self.description, 
            'port': self.port
        })
        while True:
            await self.ping_database(slug)
            await asyncio.sleep(self.ping_database_interval)

    async def ping_database(self, slug):
        await db.games.update_game(slug)


class TermninjaServer(PingDatabaseMixin,
                      OptionalAuthenticationMixin,
                      Server):
    pass
