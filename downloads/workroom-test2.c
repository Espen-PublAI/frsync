#define CREATOR "malric"
/* New workroom.c for newbie immortals.  Same basic functions
 * as the old one, but isn't going to force the entire mudlib
 * down your throat in comments.
 *
 * Radix : September 27, 1995
 */

inherit "/std/room.c";
// inherit the file "/std/outside.c" if you want clouds and such.


void setup()
{
   set_short(CREATOR+"'s workroom");

   set_long("\nWorkroom of "+CREATOR+".\n\n"
      "   The workroom you find yourself standing in has a new layer "
      "of paint.  A normal assortment of dusty furniture is "
      "arranged around the room.  The most important being the "
      "large desk and old chair against the far wall facing you.  "
      "Hanging from the ceiling, a small lamp gives ample light for "
      "the room.  With some consideration, you decide the room "
      "is in need of cleaning.  "
      "\n\n");

   // sets the level of light in the room.   "help light" for details
   set_light(80);

   //add_item() is used to describe ALL nouns you have in descriptions
   add_item("workroom","The workroom surrounds you with the smell "
      "of a fresh coat of paint.  The walls glisten in the light.\n");

   add_item("wall","All four walls are white frshly coated with "
      "paint.\n");

   add_item("paint","The entire room has been painted with a new "+
      "coat of paint.  The paint upon the walls reflects light from "
      "lamp hanging from the center of the room.\n");

   // you can also give many items the same description
   add_item(({"furniture","desk","chair"}),"Facing you against "
      "the far wall, a large desk and chair are set.  The desk and "+
      "chair have a large layer of dust covering their entire "
      "surface.  The chair has nearly worn through and perhaps "
      "is in need of repair.\n");

   add_item(({"ceiling","lamp","small lamp"}),
      "Hanging from the center of the room's ceiling, a small "
      "oil lamp burns continuously emitting ample lighting for "
      "for the room.\n");

   //SENSES by Sojan.  This adds even more life to your rooms
   add_smell(({"room","workroom","air"}),"Here we put what the "
      "player would get when they typed 'smell room'  They should "
      "smell the paint in the air of course.\n");

   add_feel("desk","Here we would put what the player would "
      "get when they typed 'touch desk'\n");

   add_taste("paint","You lick the paint from the wall and soon "
      "realize this was a major mistake...  get this by typing "
      "'taste paint'\n");

   add_sound(({"room","workroom"}),"You hear a rumbling noise "+
      "coming from above.  Get this by typing 'listen room'\n");

   //Remember, when describing your future rooms, always describe
   //everything as richly as possible (not half-baked like these)

   //The following is used to "clone" objects into rooms.
   //These objects can range from NPCs and monsters to weapons.
   //A seperate file (much like this workroom) will be required
   //so we use the function add_clone to bring them into your rooms.

   add_clone("/obj/misc/button.c",1);

   //Here are the exits from your room
   //add_exit(direction, destination, type)
   //direction - What they must type to leave that direction
   //destination - The room they will be moved to
   //type - The exit type, can be "path","corridor","door"...
   //Read /doc/roomgen/exit_type_help for more info.

   add_exit("common","/w/common","door");
   add_exit("tavern","/d/ss/daggerford/ladyluck.c","door");
   add_exit("hub","/w/malric/dev_hub/rooms/nexus.c","door");

   //This is something neat, adds an exit IN the common to this room.

   "/w/common.c"->add_exit( CREATOR,"/w/"+CREATOR+"/workroom","door");

   //This adds a sign in the workroom with some
   //basic information.  'man add_sign' will explain more about signs.
   add_sign("A large, important-looking sign.",
      "Welcome as a new creator on Final Realms!\n"
      "-----------------------------------------\n"
      "Here are some advice you might need as a new immort:\n"
      "* Read the rules!  The rules are located as a help-file:\n"
      "\t'help rules' and read the one in creators section.\n"
      "* Update your finger info so that at least the name and \n"
      "\temail address are correct.\n"
      "* Turn on the domain channel for Radish 'radish on', which is the domain you\n"
      "\tare located in until you have mastered the basics of coding.\n"
      "* The creator channel is 'cre' and functions in the same way\n"
      "\tas guild, group channels and so on.  You can ask your questions\n"
      "\ton the cre channel if you don't get answer on the radish channel,\n"
      "\tand your sponsor is not logged on.  Most immortals will be happy\n"
      "\tto help you if they have some time.\n"
      "* To get started, you should read help builder_1, help builder_2 and\n"
      "\tso on.  These help files are also located on the web:\n"
      "\thttp://james.mortencb.cx/\n"
      "* Some immortals do not like that you goto them before you ask.\n"
      "\tThis is similar to just entering a person's house without\n"
      "\tfirst knocking on the door.  It is polite to ask before goto.\n"
      "* If you want to read boards, and you should read commonroom at\n"
      "\tleast, you might want to 'clone /obj/misc/board_mas4.c'.\n"
      "* If you are going to be gone for some time (busy in RL or \n"
      "\twhatever), mail the thane of Radish informing that you have to\n"
      "\tbe away for a couple of days.  If not, you might not have an\n"
      "\timmortal to log in when you get back.\n"
      "* If you want to borrow code other's made, *always* ask them first!\n"
      "\tMost immortals will say yes if you ask them first, and\n"
      "\tinclude credit to them as a comment in the code.\n"
      "* Stay invis until you are done in Radish.\n"
      "\tWhen you use goto, you will become vis, just remember to type\n"
      "\tinvis again, or make an 'alias goto goto $*$;invis'.\n"
      "* Your project in Radish can in short be defined as this:\n"
      "\t* At least 4 rooms that are connected.\n"
      "\t* At least one hidden exit.\n"
      "\t* At least one NPC with armour and weapon(s)\n"
      "\t* At least one add_action.\n"
      "\nIf you wonder about something, do not hesitate to ask\n"
      "other's in Radish, the Thane of Radish and other immortals.\n"
      "No question is too stupid, everyone have started out knowing\n"
      "little or nothing about coding on Final Realms.\n"
      "Again, welcome as a creator on Final Realms.\n"
      "-James, Thane of Radish\n"
      "General Helper.\n"
      "\n",
      "A sign",
      "sign");


}

// This ends the basic workroom.
// For a workroom with added features such as add_action()'s
// look at workroom2.c 
// This should be in your work directory as well...
